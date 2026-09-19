"""
API Credential Vault — PART 1 (infrastructure only).

Before this phase the runtime's only credential source was one deployment
environment variable per provider, and the M2.4 STT pool documented a Supabase
Vault RPC that no migration created. This phase adds the missing infrastructure:

  1. the metadata table ``public.api_credentials`` — credential BOOKKEEPING only,
     with **no column that can hold a secret**, anchored to Supabase Vault by a
     ``vault_secret_id`` foreign key;
  2. ONE generic, provider-agnostic resolution function
     ``public.api_credential_pool(p_provider [, p_owner_id])`` — SECURITY DEFINER,
     hardened ``search_path``, service-role only — that returns the ordered,
     enabled, DECRYPTED credentials of one provider;
  3. the deprecated ``stt_credential_pool`` alias, so the name the M2.4 report
     documented keeps working;
  4. the application boundary ``backend/ai/credential_source.py`` pointed at the
     generic name, with its call shape (``{"p_provider": …}``) unchanged.

This suite pins the migration, the documentation that must match it, and the
application contract. It executes NO SQL, creates NO secret and reaches NO
database: the RPC is a fake, exactly as in the M2.4 suite. No provider, no
credential and no recognition quality is claimed here.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import re
from typing import Any

import pytest

from backend.ai import credential_source
from backend.services import stt_credential_pool

REPO = pathlib.Path(__file__).resolve().parent.parent
MIGRATION_NAME = "20260919000001_create_api_credential_vault.sql"
MIGRATION_PATH = REPO / "supabase" / "migrations" / MIGRATION_NAME
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
REPORT_PATH = REPO / "IMPLEMENTATION_REPORT.md"

MIGRATION = MIGRATION_PATH.read_text(encoding="utf-8")
DOC = DOC_PATH.read_text(encoding="utf-8")
SOURCE_MODULE = pathlib.Path(
    credential_source.__file__ or "backend/ai/credential_source.py"
).read_text(encoding="utf-8")

#: Distinctive fake secrets. Their appearance in a log line, a document or a
#: metadata payload is unambiguous.
SECRET_ENV = "sm-env-key-part1-4c1f"
SECRET_A = "sm-vault-key-a-part1-77b2"
SECRET_B = "sm-vault-key-b-part1-3d90"
SECRET_OPENAI = "openai-vault-key-part1-9ab4"
ALL_SECRETS = (SECRET_ENV, SECRET_A, SECRET_B, SECRET_OPENAI)

SPEECHMATICS = "speechmatics"
OPENAI = "openai"
SPEECHMATICS_ENV_VARS = ("AI_SPEECHMATICS_API_KEY",)

#: A secret column would have to be named something in this family. The schema is
#: asserted to declare NONE of them.
SECRET_COLUMN_PATTERNS = (
    "secret", "api_key", "apikey", "key", "token", "password", "passwd",
    "credential_secret", "session", "hash", "bearer",
)


# ── Source parsing helpers ──


def _statements(text: str) -> list[str]:
    """The executable statement lines of a SQL text (comments and blanks removed)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


def _create_table_block(table: str) -> str:
    """The body of ``CREATE TABLE IF NOT EXISTS public.<table> ( … );``."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS public\.{table} \((.*?)\n\);",
        MIGRATION,
        flags=re.S,
    )
    assert match, f"{table} must be created by the migration"
    return match.group(1)


def _column_names(table: str) -> list[str]:
    """The declared column names of a table created by the migration."""
    names: list[str] = []
    for raw in _create_table_block(table).splitlines():
        line = raw.strip()
        if not line or line.startswith("CONSTRAINT"):
            continue
        head = line.split()
        if len(head) >= 2 and re.fullmatch(r"[a-z_]+", head[0]):
            names.append(head[0])
    return names


def _section_29() -> str:
    marker = "## 29. API Credential Vault (PART 1)"
    assert marker in DOC, "DATABASE_ARCHITECTURE.md must document the vault in §29"
    return DOC[DOC.index(marker):]


def _sql_blocks(section: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", section, flags=re.S)


def _assert_no_secret(text: str, where: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, f"a raw secret leaked into {where}"


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _no_ambient_provider_keys(monkeypatch):
    """The pool reads the provider's OWN declared variables; the host must not arm one."""
    for name in (
        "AI_GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "AI_GROQ_API_KEY",
        "GROQ_API_KEY",
        "AI_SPEECHMATICS_API_KEY",
        "AI_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vault(monkeypatch):
    """Install a fake secret backend at the ONE transport seam."""

    def _install(pools: dict[str, list[Any]]) -> None:
        async def _fetch(provider: str) -> tuple[Any, ...]:
            return tuple(pools.get(provider, ()))

        monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)

    return _install


@pytest.fixture
def failing_vault(monkeypatch):
    """Install a secret backend that refuses every read."""

    async def _fetch(provider: str) -> tuple[Any, ...]:
        raise RuntimeError("the secret backend is unavailable")

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)


def _row(
    credential_id: str,
    secret: str,
    *,
    priority: int = 0,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "credential_id": credential_id,
        "priority": priority,
        "enabled": enabled,
        "secret": secret,
    }


# ══ 1. The migration: metadata table, no secret column, real constraints ══


def test_the_migration_exists_under_the_project_naming_convention():
    assert MIGRATION_PATH.is_file()
    assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", MIGRATION_NAME)
    assert MIGRATION_PATH in sorted((REPO / "supabase" / "migrations").glob("*.sql"))


def test_the_metadata_table_declares_no_column_that_can_hold_a_secret():
    """The whole point of the phase: the table holds bookkeeping, never a key."""
    columns = _column_names("api_credentials")
    assert columns, "api_credentials must declare columns"
    for name in columns:
        assert name not in SECRET_COLUMN_PATTERNS, (
            f"api_credentials.{name} looks like a plaintext secret column"
        )
    assert "vault_secret_id" in columns
    assert "secret" not in columns


def test_the_metadata_model_is_the_documented_minimum():
    columns = set(_column_names("api_credentials"))
    assert columns == {
        "credential_id",
        "provider",
        "label",
        "owner_id",
        "enabled",
        "priority",
        "vault_secret_id",
        "created_at",
        "updated_at",
    }


def test_the_metadata_row_is_anchored_to_vault_with_a_cascading_foreign_key():
    assert "REFERENCES vault.secrets(id) ON DELETE CASCADE" in MIGRATION
    assert "vault_secret_id  uuid        NOT NULL" in MIGRATION


def test_the_table_is_owner_scoped_and_deterministically_ordered():
    assert "owner_id         bigint      NOT NULL" in MIGRATION
    assert "priority         integer     NOT NULL DEFAULT 0" in MIGRATION
    assert "enabled          boolean     NOT NULL DEFAULT true" in MIGRATION


@pytest.mark.parametrize(
    "constraint",
    [
        "api_credentials_id_format",
        "api_credentials_provider_format",
        "api_credentials_label_not_blank",
        "api_credentials_owner_positive",
        "api_credentials_priority_range",
    ],
)
def test_every_declared_check_constraint_exists(constraint):
    assert f"CONSTRAINT {constraint}" in MIGRATION


def test_the_credential_id_check_matches_the_application_alphabet():
    """The schema refuses exactly what the application refuses — no drift."""
    assert r"CHECK (credential_id ~ '^[A-Za-z0-9._-]{1,64}$')" in MIGRATION
    assert credential_source._MAX_ID_LEN == 64


def test_a_vault_secret_can_map_to_only_one_credential():
    """Two names for one key would make credential rotation a silent no-op."""
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_credentials_vault_secret" in MIGRATION
    assert "ON public.api_credentials (vault_secret_id);" in MIGRATION


def test_the_provider_read_is_indexed():
    assert "idx_api_credentials_provider_order" in MIGRATION
    assert "(provider, priority, created_at, credential_id)" in MIGRATION
    assert "idx_api_credentials_owner" in MIGRATION


def test_the_table_is_rls_enabled_with_no_public_policy():
    assert "ALTER TABLE public.api_credentials ENABLE ROW LEVEL SECURITY;" in MIGRATION
    assert "CREATE POLICY" not in MIGRATION, (
        "credential bookkeeping must have no anon/authenticated policy"
    )


def test_the_table_is_revoked_from_public_roles_and_granted_to_the_service_role():
    assert (
        "REVOKE ALL ON TABLE public.api_credentials FROM PUBLIC, anon, authenticated;"
        in MIGRATION
    )
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.api_credentials "
        "TO service_role;" in MIGRATION
    )


# ══ 2. The resolution RPC: narrow, hardened, service-role only ══


def test_the_resolution_function_has_the_documented_signature_and_return_shape():
    assert "CREATE OR REPLACE FUNCTION public.api_credential_pool(" in MIGRATION
    assert "p_provider text," in MIGRATION
    assert "p_owner_id bigint DEFAULT NULL" in MIGRATION
    for column in ("credential_id text", "secret        text", "priority      integer", "enabled       boolean"):
        assert column in MIGRATION


def test_the_resolution_function_is_security_definer_with_a_hardened_search_path():
    assert "SECURITY DEFINER" in MIGRATION
    assert "SET search_path = ''" in MIGRATION, "a definer function must not trust search_path"
    assert "LANGUAGE sql" in MIGRATION
    assert "STABLE" in MIGRATION


def test_the_resolution_function_has_explicit_ownership():
    assert "ALTER FUNCTION public.api_credential_pool(text, bigint) OWNER TO postgres;" in MIGRATION
    assert "ALTER FUNCTION public.stt_credential_pool(text, bigint) OWNER TO postgres;" in MIGRATION


def test_the_resolution_function_is_not_publicly_executable():
    for signature in ("api_credential_pool(text, bigint)", "stt_credential_pool(text, bigint)"):
        assert (
            f"REVOKE ALL ON FUNCTION public.{signature}\n    FROM PUBLIC, anon, authenticated;"
            in MIGRATION
        )
        assert (
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO service_role;" in MIGRATION
        )


def test_the_resolution_read_is_bounded_ordered_and_filtered():
    assert "LIMIT 8;" in MIGRATION, "the RPC must return a bounded number of rows"
    assert (
        "ORDER BY c.priority ASC, c.created_at ASC, c.credential_id ASC" in MIGRATION
    ), "ordering must be fully deterministic"
    assert "AND c.enabled" in MIGRATION, "a disabled credential must never be returned"
    assert "AND (p_owner_id IS NULL OR c.owner_id = p_owner_id)" in MIGRATION


def test_the_resolution_reads_the_secret_only_through_vault():
    assert "JOIN vault.decrypted_secrets AS d" in MIGRATION
    assert "ON d.id = c.vault_secret_id" in MIGRATION
    assert "d.decrypted_secret AS secret" in MIGRATION
    body = MIGRATION[MIGRATION.index("CREATE OR REPLACE FUNCTION public.api_credential_pool"):]
    body = body[: body.index("$$;")]
    assert "vault.decrypted_secrets" in body
    assert "vault.secrets" not in body, "the resolver must not read the encrypted table directly"


def test_the_rpc_is_not_a_generic_sql_endpoint():
    lowered = MIGRATION.lower()
    assert "execute format" not in lowered, "no dynamic SQL"
    assert "execute '" not in lowered
    assert "vault.create_secret" not in lowered, "the migration must not create a secret"
    assert "set local role" not in lowered
    assert "security invoker" not in lowered


def test_the_deployment_keeps_the_m24_name_as_a_compatibility_alias():
    assert "CREATE OR REPLACE FUNCTION public.stt_credential_pool(" in MIGRATION
    assert "SELECT * FROM public.api_credential_pool(p_provider, p_owner_id);" in MIGRATION
    assert "Deprecated compatibility alias" in MIGRATION


def test_the_migration_asks_postgrest_to_reload():
    assert "NOTIFY pgrst, 'reload schema';" in MIGRATION


def test_the_migration_does_not_seed_a_credential():
    """No row is fabricated: the owner maps their own Vault secrets."""
    assert "vault.create_secret(" not in MIGRATION
    assert "INSERT INTO public.api_credentials" not in MIGRATION


# ══ 3. The documentation must match the migration exactly ══


def test_the_documentation_documents_the_table_and_the_rpc():
    section = _section_29()
    assert "### 29.2 `api_credentials`" in section
    assert "### 29.3 `api_credential_pool" in section
    assert "### 29.4 `stt_credential_pool" in section
    assert "### 29.11 Manual rollback SQL" in section


def test_the_documented_manual_sql_is_statement_identical_to_the_migration():
    """§29.10 is not prose about the migration — it IS the migration."""
    section = _section_29()
    manual = section[section.index("### 29.10 Manual Supabase SQL"):]
    block = _sql_blocks(manual)[0]
    assert _statements(block) == _statements(MIGRATION)


def test_the_documentation_labels_the_sql_as_not_executed():
    section = _section_29()
    assert "NOT EXECUTED BY AI" in section
    assert section.count("NOT EXECUTED BY AI") >= 2, "both the SQL and the rollback carry the label"
    assert "NOTHING was executed against" in section


def test_the_documented_rollback_is_complete_and_scoped():
    section = _section_29()
    rollback = _sql_blocks(section[section.index("### 29.11"):])[0]
    assert "DROP FUNCTION IF EXISTS public.stt_credential_pool(text, bigint);" in rollback
    assert "DROP FUNCTION IF EXISTS public.api_credential_pool(text, bigint);" in rollback
    assert "DROP TABLE IF EXISTS public.api_credentials;" in rollback
    assert "DROP EXTENSION" not in rollback, "the shared Vault extension must survive a rollback"
    assert "DROP SCHEMA" not in rollback


def _flat(section: str) -> str:
    """Prose assertions must not depend on where the document happens to wrap."""
    return re.sub(r"\s+", " ", section)


def test_the_documentation_states_that_no_secret_is_stored_in_the_table():
    flat = _flat(_section_29())
    assert "**Does it contain secrets? NO.**" in flat
    assert "no column of any name that can hold an API key, token, password or session string" in flat
    assert "The raw key exists **only** inside Supabase Vault" in flat
    assert "this table stores a *reference* to it." in flat


def test_no_database_object_is_left_undocumented():
    """Every object the migration creates is named in §29."""
    section = _section_29()
    for obj in (
        "api_credentials",
        "api_credential_pool",
        "stt_credential_pool",
        "idx_api_credentials_provider_order",
        "idx_api_credentials_owner",
        "uq_api_credentials_vault_secret",
        "supabase_vault",
    ):
        assert obj in section, f"{obj} is created but undocumented"


def test_the_documented_vault_story_covers_environment_stt_and_tts():
    section = _section_29()
    for topic in (
        "### 29.6 Security model",
        "### 29.7 ENV compatibility (unchanged)",
        "### 29.8 Failure behaviour and orphan handling",
        "### 29.9 Application consumers",
    ):
        assert topic in section
    assert "TTS" in section or "Text-to-Speech" in section
    assert "openai" in section


# ══ 4. Application boundary: the generic name, the unchanged call shape ══


def test_the_application_targets_the_generic_rpc():
    assert credential_source.VAULT_RPC == "api_credential_pool"
    assert credential_source.LEGACY_VAULT_RPC == "stt_credential_pool"


def test_the_m24_call_shape_is_unchanged(monkeypatch):
    """The runtime still sends exactly ``{"p_provider": …}`` — owner filter omitted."""
    calls: list[tuple[str, Any]] = []

    class _FakeDb:
        def rpc(self, name: str, params: Any) -> "_FakeDb":
            calls.append((name, params))
            return self

        def execute(self) -> Any:
            return type("_R", (), {"data": [_row("sm-a", SECRET_A)]})()

    monkeypatch.setattr("backend.db.client.get_db", lambda: _FakeDb())

    assert credential_source._vault_rows_sync(SPEECHMATICS) == (
        _row("sm-a", SECRET_A),
    )
    assert calls == [("api_credential_pool", {"p_provider": SPEECHMATICS})]


def test_the_legacy_name_is_never_the_runtime_target():
    """The runtime calls the generic function; the old name is a documented alias."""
    assert credential_source.LEGACY_VAULT_RPC != credential_source.VAULT_RPC
    assert SOURCE_MODULE.count('"stt_credential_pool"') == 1, (
        "the legacy name must appear once, as the documented alias constant"
    )
    assert 'LEGACY_VAULT_RPC = "stt_credential_pool"' in SOURCE_MODULE
    assert 'rpc("stt_credential_pool"' not in SOURCE_MODULE
    assert "VAULT_RPC" in SOURCE_MODULE.split(".rpc(")[1], "the RPC call must use the constant"


def test_the_secret_boundary_is_provider_generic_and_not_stt_coupled():
    """No provider list, no STT import: the caller supplies provider and names."""
    tree = ast.parse(SOURCE_MODULE)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for module in imported:
        assert not module.startswith("backend.bot"), module
        assert not module.startswith("backend.services.stt"), module
        assert "telethon" not in module, module
    assert "speechmatics" not in SOURCE_MODULE.lower()
    assert "groq" not in SOURCE_MODULE.lower()
    assert "gemini" not in SOURCE_MODULE.lower()


@pytest.mark.asyncio
async def test_a_non_stt_provider_can_resolve_its_own_pool(monkeypatch, vault):
    """The same table, RPC and boundary serve a future TTS provider."""
    vault({OPENAI: [_row("openai-a", SECRET_OPENAI, priority=5)]})
    monkeypatch.setenv("AI_OPENAI_API_KEY", "openai-env-key-part1")

    records = await credential_source.load(OPENAI, ("AI_OPENAI_API_KEY", "OPENAI_API_KEY"))

    assert [record.credential_id for record in records] == [
        f"{credential_source.SOURCE_ENV}:AI_OPENAI_API_KEY",
        f"{credential_source.SOURCE_VAULT}:openai-a",
    ]
    assert records[0].secret == "openai-env-key-part1"
    assert records[1].secret == SECRET_OPENAI
    assert credential_source.cached(SPEECHMATICS) is None, "one provider's pool must not leak"


# ══ 5. Row validation: a half-configured store degrades, never fails ══


@pytest.mark.asyncio
async def test_a_malformed_response_contributes_nothing(monkeypatch, vault):
    vault({SPEECHMATICS: [{"secret": SECRET_A}]})  # no credential_id
    assert await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS) == ()


@pytest.mark.asyncio
async def test_a_response_that_is_not_a_list_contributes_nothing(monkeypatch):
    async def _fetch(provider: str) -> tuple[Any, ...]:
        return ("not", "a", "list", "of", "rows")

    monkeypatch.setattr(credential_source, "_fetch_rows", _fetch)
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert [record.credential_id for record in records] == [
        f"{credential_source.SOURCE_ENV}:AI_SPEECHMATICS_API_KEY"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_row("has space", SECRET_A)],
        [_row("semi;colon", SECRET_A)],
        [_row("a" * 65, SECRET_A)],
        [_row("sm-a", "")],
        [_row("sm-a", SECRET_A, enabled=False)],
        [["a", "list", "not", "a", "row"]],
        [None],
        [{}],
    ],
)
async def test_every_unusable_vault_row_is_skipped_rather_than_repaired(monkeypatch, vault, rows):
    vault({SPEECHMATICS: rows})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)

    identifiers = [record.credential_id for record in records]
    assert identifiers[0] == f"{credential_source.SOURCE_ENV}:AI_SPEECHMATICS_API_KEY"
    assert len(identifiers) == 1, "an unusable row must contribute no credential"


@pytest.mark.asyncio
async def test_a_row_with_a_bad_priority_still_contributes_with_the_default(monkeypatch, vault):
    vault({SPEECHMATICS: [{"credential_id": "sm-a", "secret": SECRET_A, "priority": "x"}]})
    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert [(record.credential_id, record.priority) for record in records] == [
        (f"{credential_source.SOURCE_VAULT}:sm-a", 0)
    ]


# ══ 6. Failure behaviour: bounded, honest, never fatal ══


@pytest.mark.asyncio
async def test_a_missing_or_refusing_rpc_is_a_bounded_warning(monkeypatch, failing_vault, caplog):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    with caplog.at_level(logging.WARNING):
        records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)

    assert [record.credential_id for record in records] == [
        f"{credential_source.SOURCE_ENV}:AI_SPEECHMATICS_API_KEY"
    ]
    assert "STT_CREDENTIAL_VAULT_READ_FAILED" in caplog.text
    assert "the secret backend is unavailable" not in caplog.text, "no raw provider error"
    _assert_no_secret(caplog.text, "the failure warning")


@pytest.mark.asyncio
async def test_an_unconfigured_database_yields_no_rows_and_no_exception(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    assert credential_source._vault_rows_sync(SPEECHMATICS) == ()


@pytest.mark.asyncio
async def test_a_failure_never_raises_and_still_marks_the_provider_loaded(monkeypatch, failing_vault):
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert credential_source.is_loaded(SPEECHMATICS) is True


@pytest.mark.asyncio
async def test_a_refusing_backend_never_reduces_the_credentials_already_served(
    monkeypatch, vault, failing_vault
):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A)]})
    first = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    credential_source.mark_stale(SPEECHMATICS)
    second = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert [r.credential_id for r in second] == [r.credential_id for r in first]


# ══ 7. Ordering, bounds and the ENV fallback ══


@pytest.mark.asyncio
async def test_the_environment_credential_stays_first_and_the_vault_follows(monkeypatch, vault):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A, priority=1), _row("sm-b", SECRET_B, priority=2)]})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)

    assert [record.source for record in records] == [
        credential_source.SOURCE_ENV,
        credential_source.SOURCE_VAULT,
        credential_source.SOURCE_VAULT,
    ]
    assert records[0].secret == SECRET_ENV


@pytest.mark.asyncio
async def test_an_explicit_vault_priority_may_outrank_the_environment(monkeypatch, vault):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A, priority=-1)]})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert records[0].credential_id == f"{credential_source.SOURCE_VAULT}:sm-a"


@pytest.mark.asyncio
async def test_an_empty_backend_keeps_the_environment_credential(monkeypatch, vault):
    vault({})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert [record.source for record in records] == [credential_source.SOURCE_ENV]


@pytest.mark.asyncio
async def test_no_environment_credential_leaves_only_the_vault_pool(vault):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A)]})
    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert [record.source for record in records] == [credential_source.SOURCE_VAULT]


@pytest.mark.asyncio
async def test_the_pool_is_bounded_per_provider(monkeypatch, vault):
    vault(
        {
            SPEECHMATICS: [
                _row(f"sm-{index}", f"secret-{index}") for index in range(10)
            ]
        }
    )
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)
    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert len(records) == credential_source.MAX_CREDENTIALS_PER_PROVIDER == 4


@pytest.mark.asyncio
async def test_the_environment_is_never_scanned_for_an_undeclared_name(monkeypatch, vault):
    vault({})
    monkeypatch.setenv("SPEECHMATICS_KEY_1", SECRET_A)
    monkeypatch.setenv("SM_KEY_2", SECRET_B)
    records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)
    assert records == ()


# ══ 8. Secrets never leak — not into logs, not into metadata, not into a document ══


@pytest.mark.asyncio
async def test_no_raw_secret_reaches_a_log_line_on_the_success_path(monkeypatch, vault, caplog):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A, priority=1), _row("sm-b", SECRET_B, priority=2)]})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    with caplog.at_level(logging.INFO):
        records = await credential_source.load(SPEECHMATICS, SPEECHMATICS_ENV_VARS)

    assert len(records) == 3
    assert "STT_CREDENTIAL_POOL_LOADED" in caplog.text
    _assert_no_secret(caplog.text, "the load log")
    described = stt_credential_pool.describe(SPEECHMATICS)
    _assert_no_secret(described, "the pool description")


def _module_code() -> str:
    """The module's executable source, with its docstring removed."""
    tree = ast.parse(SOURCE_MODULE)
    docstring = ast.get_docstring(tree)
    if docstring:
        return SOURCE_MODULE.replace(docstring, "", 1)
    return SOURCE_MODULE


def test_the_boundary_never_writes_a_secret_to_a_table():
    """Metadata is never persisted by this module: the RPC is its only database call."""
    code = _module_code()
    assert ".rpc(" in code
    for forbidden in (".table(", ".insert(", ".upsert(", ".update(", ".delete("):
        assert forbidden not in code, f"the secret boundary must not call {forbidden}"
    assert "vault.decrypted_secrets" not in code, "Vault internals stay behind the RPC"
    assert "vault." not in code, "the boundary must not read the Vault schema directly"


def test_the_migration_stores_no_secret_value():
    _assert_no_secret(MIGRATION, "the migration")


def test_the_documents_carry_no_secret():
    _assert_no_secret(DOC, "DATABASE_ARCHITECTURE.md")
    _assert_no_secret(REPORT_PATH.read_text(encoding="utf-8"), "IMPLEMENTATION_REPORT.md")


def test_no_credential_value_lookalike_is_committed_anywhere_in_the_change_set():
    """A real key has a recognisable shape; none may appear in the new artefacts."""
    pattern = re.compile(r"(sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,})")
    for path in (MIGRATION_PATH, DOC_PATH, REPORT_PATH):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


# ══ 9. Owner scoping ══


def test_the_metadata_is_owner_scoped_at_the_database_boundary():
    assert "owner_id         bigint      NOT NULL" in MIGRATION
    assert "api_credentials_owner_positive" in MIGRATION
    assert "AND (p_owner_id IS NULL OR c.owner_id = p_owner_id)" in MIGRATION
    assert "p_owner_id bigint DEFAULT NULL" in MIGRATION


def test_the_runtime_omits_the_owner_filter_so_single_owner_calls_are_unchanged():
    """The M2.4 call carried only ``p_provider``; it still does."""
    assert '{"p_provider": provider}' in SOURCE_MODULE
    assert "p_owner_id" not in SOURCE_MODULE


# ══ 10. M2.4 STT compatibility ══


@pytest.mark.asyncio
async def test_the_stt_pool_still_loads_through_the_generic_boundary(monkeypatch, vault):
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A)]})
    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_ENV)

    counts = await stt_credential_pool.prepare([SPEECHMATICS])

    assert counts == {SPEECHMATICS: 2}
    assert stt_credential_pool.is_configured(SPEECHMATICS) is True
    assert [record.credential_id for record in stt_credential_pool.credentials_for(SPEECHMATICS)] == [
        f"{credential_source.SOURCE_ENV}:AI_SPEECHMATICS_API_KEY",
        f"{credential_source.SOURCE_VAULT}:sm-a",
    ]


@pytest.mark.asyncio
async def test_a_vault_credential_still_rotates_inside_the_provider(monkeypatch, vault):
    """Credential-level fallback is driven by the SAME pool, now Vault-backed."""
    vault(
        {
            SPEECHMATICS: [
                _row("sm-a", SECRET_A, priority=1),
                _row("sm-b", SECRET_B, priority=2),
            ]
        }
    )
    await stt_credential_pool.prepare([SPEECHMATICS])

    rotation = stt_credential_pool.rotation_for(SPEECHMATICS)
    assert [record.credential_id for record in rotation] == [
        f"{credential_source.SOURCE_VAULT}:sm-a",
        f"{credential_source.SOURCE_VAULT}:sm-b",
    ]

    stt_credential_pool.record_failure(rotation[0].credential_id, "auth")

    assert [r.credential_id for r in stt_credential_pool.rotation_for(SPEECHMATICS)] == [
        f"{credential_source.SOURCE_VAULT}:sm-b"
    ]
    assert stt_credential_pool.is_cooled_down(rotation[0].credential_id) is True


@pytest.mark.asyncio
async def test_an_exhausted_vault_pool_still_hands_over_to_the_provider_layer(monkeypatch, vault):
    """All credentials cooling down must not become a hard media failure."""
    vault({SPEECHMATICS: [_row("sm-a", SECRET_A)]})
    await stt_credential_pool.prepare([SPEECHMATICS])

    for record in stt_credential_pool.credentials_for(SPEECHMATICS):
        stt_credential_pool.record_failure(record.credential_id, "auth")

    assert stt_credential_pool.rotation_for(SPEECHMATICS) != (), (
        "the provider must still be attempted so the provider fallback can run"
    )
    assert stt_credential_pool.registered_providers(), "the provider registry is intact"


def test_the_stt_classification_vocabulary_is_unchanged():
    from backend.services.media_service import MediaError

    credential_specific = MediaError("rejected")
    credential_specific.failure_class = "auth"
    assert stt_credential_pool.is_credential_specific(credential_specific) is True

    provider_wide = MediaError("provider exploded")
    provider_wide.failure_class = "server"
    provider_wide.http_status = 503
    assert stt_credential_pool.is_credential_specific(provider_wide) is False

    programming_error = ValueError("a bug, not a provider")
    assert stt_credential_pool.is_credential_specific(programming_error) is False
