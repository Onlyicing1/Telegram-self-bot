"""The Auto-Reaction columns: migration semantics, idempotency and the OFF default.

``backend/services/settings_service.py`` reads and writes
``panel_settings.auto_reaction_enabled`` / ``auto_reaction_emoji``. No earlier
migration created them, so the write was rejected and the setting survived only
in the process cache — the reported "resets after a redeploy".

This suite executes the SHIPPED migration statement by statement against a small
column/row model with PostgreSQL's semantics:

* ``ADD COLUMN IF NOT EXISTS`` is a no-op when the column exists (an existing
  column keeps its values);
* ``ALTER COLUMN … SET DEFAULT`` binds the default but rewrites no row;
* ``UPDATE … WHERE <col> IS NULL`` backfills only NULL cells;
* ``ALTER COLUMN … SET NOT NULL`` fails when a NULL survives.

The model refuses a statement form it does not understand, so the migration
cannot silently grow a step the suite does not verify. No database is contacted.
"""
from __future__ import annotations

import re
from pathlib import Path

from backend.services import settings_service

REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "supabase" / "migrations" / "20261020160000_add_panel_settings_auto_reaction_settings.sql"
DOC = REPO / "DATABASE_ARCHITECTURE.md"

COLUMNS = ("auto_reaction_enabled", "auto_reaction_emoji")


# ── a column/row model with the relevant PostgreSQL semantics ──────────────

class _Unsupported(Exception):
    pass


def _statements(sql: str) -> list[str]:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    sql = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    # The migration carries no semicolon inside a string literal.
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def _literal(expr: str):
    expr = expr.strip()
    if expr == "false":
        return False
    if expr == "true":
        return True
    if expr == "''":
        return ""
    raise _Unsupported(f"unsupported literal: {expr!r}")


def _apply(columns: dict[str, dict], rows: list[dict], statements: list[str]) -> None:
    for statement in statements:
        s = " ".join(statement.split())

        if s.upper().startswith(("NOTIFY ", "SELECT ")):
            continue

        match = re.fullmatch(
            r"ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS (\w+) (\w+)", s
        )
        if match:
            name, type_ = match.groups()
            if name not in columns:
                columns[name] = {"type": type_, "default": None, "not_null": False}
                for row in rows:
                    row[name] = None
            continue

        match = re.fullmatch(
            r"ALTER TABLE panel_settings ALTER COLUMN (\w+) SET DEFAULT (.+)", s
        )
        if match:
            name, value = match.groups()
            assert name in columns, f"SET DEFAULT on a missing column: {name}"
            columns[name]["default"] = value
            # PostgreSQL does NOT rewrite existing rows for a new default.
            continue

        match = re.fullmatch(
            r"UPDATE panel_settings SET (\w+) = (.+) WHERE \1 IS NULL", s
        )
        if match:
            name, value = match.groups()
            assert name in columns, f"UPDATE on a missing column: {name}"
            for row in rows:
                if row.get(name) is None:
                    row[name] = _literal(value)
            continue

        match = re.fullmatch(
            r"ALTER TABLE panel_settings ALTER COLUMN (\w+) SET NOT NULL", s
        )
        if match:
            name = match.group(1)
            assert name in columns, f"SET NOT NULL on a missing column: {name}"
            assert all(row.get(name) is not None for row in rows), (
                f"{name} still has a NULL after the backfill"
            )
            columns[name]["not_null"] = True
            continue

        raise _Unsupported(f"the suite does not model this statement: {statement!r}")


def _run_once(columns: dict[str, dict], rows: list[dict]) -> None:
    _apply(columns, rows, _statements(MIGRATION.read_text(encoding="utf-8")))


# ── the migration file itself ───────────────────────────────────────────────

def test_the_migration_is_additive_idempotent_and_never_enables_a_reaction():
    statements = _statements(MIGRATION.read_text(encoding="utf-8"))
    assert statements, "the migration must carry executable SQL"
    joined = "\n".join(statements).upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in joined, f"the migration must stay additive: {forbidden}"
    assert "SET DEFAULT TRUE" not in joined, "applying the schema must never enable it"
    assert "= TRUE" not in joined
    # Every ADD must be a re-runnable IF NOT EXISTS.
    for statement in statements:
        if "ADD COLUMN" in statement.upper():
            assert "ADD COLUMN IF NOT EXISTS" in statement.upper(), statement
    for column in COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in "\n".join(statements)


def test_a_legacy_row_keeps_every_other_setting_and_gains_the_off_defaults():
    columns = {
        "key": {"type": "text", "default": None, "not_null": True},
        "auto_close_enabled": {"type": "boolean", "default": "true", "not_null": True},
        "language": {"type": "text", "default": "'en'", "not_null": True},
    }
    rows = [{"key": "global", "auto_close_enabled": False, "language": "fa"}]

    _run_once(columns, rows)

    row = rows[0]
    assert row["auto_close_enabled"] is False and row["language"] == "fa", (
        "an unrelated stored setting must never be rewritten"
    )
    assert row["auto_reaction_enabled"] is False, "the default is OFF"
    assert row["auto_reaction_emoji"] == "", "'' means none selected"
    assert columns["auto_reaction_enabled"] == {
        "type": "boolean", "default": "false", "not_null": True,
    }
    assert columns["auto_reaction_emoji"] == {
        "type": "text", "default": "''", "not_null": True,
    }


def test_a_re_run_is_a_no_op_and_an_existing_value_is_never_replaced():
    # A database where the columns already exist (an earlier out-of-band add)
    # and the owner has already turned the feature ON.
    columns: dict[str, dict] = {
        "auto_reaction_enabled": {"type": "boolean", "default": None, "not_null": False},
        "auto_reaction_emoji": {"type": "text", "default": None, "not_null": False},
    }
    rows = [{"auto_reaction_enabled": True, "auto_reaction_emoji": "🎉"}]

    _run_once(columns, rows)
    _run_once(columns, rows)  # the owner re-runs the one-paste setup

    assert rows == [{"auto_reaction_enabled": True, "auto_reaction_emoji": "🎉"}], (
        "a stored value (even a non-default one) must survive a re-run"
    )


def test_only_a_null_cell_is_backfilled():
    columns = {
        "auto_reaction_enabled": {"type": "boolean", "default": "false", "not_null": False},
        "auto_reaction_emoji": {"type": "text", "default": "''", "not_null": False},
    }
    rows = [
        {"auto_reaction_enabled": True, "auto_reaction_emoji": None},
        {"auto_reaction_enabled": None, "auto_reaction_emoji": "🎉"},
    ]

    _run_once(columns, rows)

    assert rows == [
        {"auto_reaction_enabled": True, "auto_reaction_emoji": ""},
        {"auto_reaction_enabled": False, "auto_reaction_emoji": "🎉"},
    ]


def test_the_migration_matches_the_canonical_documentation():
    doc = DOC.read_text(encoding="utf-8")
    assert MIGRATION.name in doc, "the migration must be named in the architecture doc"
    section_6 = doc[doc.index("## 6. panel_settings"):]
    section_6 = section_6[: section_6.index("\n## ", 10)]
    assert "auto_reaction_enabled" in section_6
    assert "auto_reaction_emoji" in section_6


# ── the application contract ────────────────────────────────────────────────

def test_the_service_default_is_off():
    assert settings_service._DEFAULTS["auto_reaction_enabled"] is False
    assert settings_service._DEFAULTS["auto_reaction_emoji"] == ""
    assert settings_service.is_auto_reaction_enabled() is False
    assert settings_service.auto_reaction_emoji() == ""


def test_the_service_writes_the_real_columns(monkeypatch):
    written: dict[str, object] = {}

    def _update(field, value):
        written[field] = value
        return True

    monkeypatch.setattr("backend.services.panel_settings_repository.update_field", _update)
    monkeypatch.setattr("backend.services.panel_settings_repository.load", lambda: None)

    assert settings_service.set_auto_reaction_enabled(True) is True
    assert settings_service.set_auto_reaction_emoji("🎉") is True
    assert written == {"auto_reaction_enabled": True, "auto_reaction_emoji": "🎉"}

    # The setter requires a real emoji; "none selected" is the stored default,
    # written by the panel's own clear action rather than through this setter.
    assert settings_service.set_auto_reaction_emoji("") is False
