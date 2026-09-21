"""M3.1 — canonical schema reconciliation & schema-drift regression.

Context. The canonical Supabase specification used `CREATE TABLE IF NOT EXISTS`
as its ONLY mechanism for establishing a table's shape, and then immediately ran
statements that referenced columns. On a database whose table already existed in
an older shape the CREATE is a silent no-op, and the later statement fails. The
observed production failure was:

    ERROR: 42703
    column "value_type" of relation "bot_settings" does not exist
    INSERT INTO bot_settings (key, value, value_type) VALUES ...

These tests pin the class, not the one symptom. No PostgreSQL server is
available in the build environment, so the shipped SQL is validated two ways:

1. STATIC — the two repository copies of the canonical script must be
   byte-identical (the migration and `supabase/canonical_bootstrap.sql`) and its
   §31.3 embed must be statement-identical to the migration; every table's CREATE
   column set must equal its `ADD COLUMN IF NOT EXISTS` set and its drift-report
   set.
2. SIMULATED EXECUTION — `_apply()` parses the real statements of the shipped
   script and applies them to a schema model (empty, legacy or already-canonical)
   with the semantics PostgreSQL actually uses:
     * `CREATE TABLE IF NOT EXISTS` is a NO-OP when the table exists;
     * a statement referencing a column that the model does not have is a
       FAILURE, exactly like 42703;
     * `INSERT` into a NOT NULL column without a default must provide a value;
     * `SET NOT NULL` fails when a NULL survives the backfills;
     * `ON CONFLICT DO NOTHING` skips rows whose primary key already exists.
   The simulator refuses to skip a statement form it does not understand, so
   coverage cannot silently regress.

This is NOT a substitute for running the script against a real database; §30.12
of DATABASE_ARCHITECTURE.md states that limitation explicitly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "DATABASE_ARCHITECTURE.md"
MIGRATION = REPO / "supabase" / "migrations" / "20260920000001_reconcile_canonical_schema.sql"
BOOTSTRAP = REPO / "supabase" / "canonical_bootstrap.sql"
MIGRATIONS_DIR = REPO / "supabase" / "migrations"

CANONICAL_TABLES = (
    "saved_items",
    "bio_state",
    "username_state",
    "bot_logs",
    "panel_settings",
    "bot_settings",
    "ai_config",
    "ai_sessions",
    "ai_messages",
    "ai_memories",
    "ai_tool_history",
    "ai_usage",
    "ai_provider_stats",
    "ghost_chats",
    "ai_tasks",
    "ai_task_occurrences",
)

# Tables the canonical contract deliberately does NOT contain (§30.2).
NON_CANONICAL_TABLES = ("ai_preferences",)

# Identifiers that are SQL keywords/functions, not columns.
_NON_COLUMN_TOKENS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in", "between",
    "count", "min", "max", "sum", "distinct", "true", "false", "exists", "case",
    "when", "then", "else", "end", "as", "on", "using", "check", "constraint",
    "length", "btrim", "octet_length", "jsonb_typeof", "jsonb_array_length",
    "coalesce", "lpad", "now", "array", "any", "public", "information_schema",
    "columns", "table_name", "table_schema", "column_name", "v", "tbl", "col",
    "t", "o", "c", "n", "oid", "conname", "contype", "conrelid", "relname",
    "relnamespace", "relkind", "selectivity", "relid", "attname", "attrelid",
    "attnum", "unnest", "string_to_array", "format", "execute", "raise",
    "warning", "notice", "exception", "if", "elsif", "loop", "for", "record",
    "boolean", "text", "integer", "bigint", "smallint", "real", "jsonb",
    "timestamptz", "bigserial", "pg_namespace", "pg_class", "pg_constraint",
    "pg_index", "pg_attribute", "pg_catalog", "primary", "key", "unique",
    "foreign", "references", "delete", "restrict", "cascade", "set", "default",
    "with", "values", "insert", "into", "update", "alter", "table", "add",
    "column", "policy", "grant", "drop", "order", "by", "asc", "desc", "left",
    "join", "group", "having", "offset", "limit", "do", "nothing", "conflict",
    "returning", "to", "role", "service_role", "anon", "authenticated", "postgres",
}


# ─── SQL statement splitting ─────────────────────────────────────────────────

def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, honouring quotes, dollar-quotes and comments."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    dollar: str | None = None
    line_comment = False
    block_depth = 0
    while i < n:
        ch = sql[i]
        if line_comment:
            buf.append(ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_depth:
            buf.append(ch)
            if sql.startswith("*/", i):
                buf.append("/")
                i += 2
                block_depth -= 1
                continue
            if sql.startswith("/*", i):
                buf.append("*")
                i += 2
                block_depth += 1
                continue
            i += 1
            continue
        if dollar:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if sql.startswith("--", i):
            line_comment = True
            buf.append("--")
            i += 2
            continue
        if sql.startswith("/*", i):
            block_depth = 1
            buf.append("/*")
            i += 2
            continue
        if ch == "'":
            quote = "'"
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            quote = '"'
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf)
    if tail.strip():
        out.append(tail)
    return [s.strip() for s in out if s.strip()]


def strip_comments(stmt: str) -> str:
    stmt = re.sub(r"/\*.*?\*/", " ", stmt, flags=re.S)
    return "\n".join(
        line for line in stmt.split("\n") if not line.strip().startswith("--")
    ).strip()


def canonical_statements() -> list[str]:
    return split_statements(MIGRATION.read_text(encoding="utf-8"))


def _strip_paren_constructs(body: str, keyword: str) -> str:
    s = body
    while True:
        m = re.search(rf"{keyword}\s*\(", s)
        if not m:
            return s
        j, depth = m.end(), 1
        while depth and j < len(s):
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
            j += 1
        s = s[: m.start()] + s[j:]


def parse_create_body(body: str) -> list[str]:
    """Column names declared by a CREATE TABLE body (constraints removed)."""
    body = re.sub(r"--[^\n]*", "", body)
    body = re.sub(r"CONSTRAINT\s+\w+\s+", "", body)
    body = _strip_paren_constructs(body, "CHECK")
    body = _strip_paren_constructs(body, "PRIMARY KEY")
    cols: list[str] = []
    for part in re.split(r",\s*\n", body):
        part = part.strip()
        if not part:
            continue
        first = part.split()[0].strip(",")
        if re.fullmatch(r"[a-z_][a-z0-9_]*", first):
            cols.append(first)
    return cols


def declared_column_sets(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        out[m.group(1)] = parse_create_body(m.group(2))
    return out


def added_column_sets(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", sql):
        out.setdefault(m.group(1), []).append(m.group(2))
    return out


def drift_report_pairs(sql: str) -> list[tuple[str, str]]:
    m = re.search(r"FROM \(VALUES\n(.*?)\n\) AS v\(tbl, col\)", sql, re.S)
    assert m, "the canonical script must end with the drift report VALUES block"
    return re.findall(r"\('([a-z_]+)','([a-z_]+)'\)", m.group(1))


def sql_blocks(text: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", text, re.S)


def section_30() -> str:
    marker = "## 30. Canonical Schema Reconciliation & Drift Repair"
    assert marker in DOC.read_text(encoding="utf-8"), "DATABASE_ARCHITECTURE.md must carry §30"
    doc = DOC.read_text(encoding="utf-8")
    start = doc.index(marker)
    nxt = doc.find("\n## ", start + len(marker))
    return doc[start:] if nxt == -1 else doc[start:nxt]


# ─── schema model ────────────────────────────────────────────────────────────

class Column:
    __slots__ = ("type", "not_null", "default")

    def __init__(self, type_: str, not_null: bool = False, default: str | None = None):
        self.type = type_
        self.not_null = not_null
        self.default = default


class Table:
    def __init__(self, name: str):
        self.name = name
        self.columns: dict[str, Column] = {}
        self.rows: list[dict[str, object]] = []
        self.primary_key: list[str] = []
        self.indexes: set[str] = set()

    @property
    def column_names(self) -> list[str]:
        return list(self.columns)


class Schema:
    def __init__(self):
        self.tables: dict[str, Table] = {}
        self.warnings: list[str] = []

    def add_table(self, name: str, columns: list[tuple[str, str, bool, str | None]]) -> Table:
        t = self.tables.setdefault(name, Table(name))
        for col, type_, not_null, default in columns:
            t.columns.setdefault(col, Column(type_, not_null, default))
        return t

    def insert_row(self, table: str, values: dict[str, object]) -> None:
        t = self.tables[table]
        row: dict[str, object] = {}
        for name, col in t.columns.items():
            row[name] = values.get(name, _default_value(col.default) if col.default is not None else None)
        t.rows.append(row)

    def contains(self, table: str, column: str) -> bool:
        return table in self.tables and column in self.tables[table].columns


def _default_value(default: str | None) -> object:
    if default is None:
        return None
    d = re.sub(r"\s*::\w+\s*$", "", default.strip())
    if d.startswith("'") and d.endswith("'"):
        return d[1:-1].replace("''", "'")
    low = d.lower()
    if low in ("true", "false"):
        return low == "true"
    if low.startswith("now()"):
        return "NOW"
    if low.startswith("nextval"):
        return "NEXTVAL"
    try:
        return float(d) if "." in d else int(d)
    except ValueError:
        return d


# ─── simulator ───────────────────────────────────────────────────────────────

_TYPE_WORDS = (
    "bigserial", "timestamptz", "smallint", "boolean", "jsonb", "integer",
    "bigint", "text", "real", "numeric",
)


def _norm(stmt: str) -> str:
    return re.sub(r"\s+", " ", stmt).strip()


def _split_add_column(rest: str) -> tuple[str, str, bool, str | None]:
    """`<type> [NOT NULL] [DEFAULT <expr>]` → (type, type, not_null, default)."""
    not_null = bool(re.search(r"\bNOT\s+NULL\b", rest, re.I))
    default = None
    m = re.search(r"\bDEFAULT\s+(.+)$", rest, re.I | re.S)
    if m:
        default = m.group(1).strip()
    type_ = rest
    for kw in ("NOT NULL", "DEFAULT"):
        idx = re.search(rf"\b{kw}\b", type_, re.I)
        if idx:
            type_ = type_[: idx.start()]
    type_ = type_.strip().rstrip(",")
    for word in _TYPE_WORDS:
        if re.match(rf"^{word}\b", type_):
            type_ = word
            break
    return type_, type_, not_null, default


def _balanced(text: str, start: int) -> tuple[str, int]:
    assert text[start] == "(", f"expected '(' at {start}: {text[start:start+40]!r}"
    depth, i = 1, start + 1
    while depth and i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start + 1 : i - 1], i


def constraint_adds(block: str) -> list[tuple[str, str, str, str]]:
    """(table, constraint_name, kind, body) for every ADD CONSTRAINT in a DO block."""
    out: list[tuple[str, str, str, str]] = []
    for m in re.finditer(r"ALTER TABLE (\w+) ADD CONSTRAINT (\w+) ", block):
        i = m.end()
        kind_m = re.match(r"(CHECK|FOREIGN KEY|UNIQUE|PRIMARY KEY)", block[i:])
        if not kind_m:
            continue
        kind = kind_m.group(1)
        i += kind_m.end()
        while i < len(block) and block[i] not in "(":
            i += 1
        if i >= len(block):
            continue
        body, _ = _balanced(block, i)
        out.append((m.group(1), m.group(2), kind, body))
    return out


def constraint_drops(block: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(r"ALTER TABLE (\w+) DROP CONSTRAINT IF EXISTS (\w+)", block)
    ]


def _check_identifiers(schema: Schema, table: str, expr: str, where: str) -> None:
    expr = re.sub(r"'[^']*'", " ", expr)
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        low = token.lower()
        if low in _NON_COLUMN_TOKENS:
            continue
        assert low not in CANONICAL_TABLES, f"{where}: CHECK references the table {low}"
        assert schema.contains(table, low), (
            f"{where}: {table}.{low} is referenced by a constraint but the column "
            f"does not exist — this is the 42703 class"
        )


def _resolve(expr: str, schema: Schema, row_index: int) -> object:
    """Deterministic value of a well-known backfill expression (strict)."""
    e = expr.strip()
    e = re.sub(r"::jsonb$|::text$", "", e).strip()
    if e.startswith("'") and e.endswith("'"):
        return e[1:-1].replace("''", "'")
    low = e.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low.startswith("now()"):
        return "NOW"
    m = re.fullmatch(r"'S' \|\| lpad\(id::text, 4, '0'\)", expr.strip())
    if m:
        return "S%04d" % (row_index + 1)
    m = re.fullmatch(r"'recovered-' \|\| id", expr.strip())
    if m:
        return "recovered-%d" % (row_index + 1)
    m = re.fullmatch(r"COALESCE\(\(SELECT min\(id\) FROM ai_tasks\), 0\)", expr.strip())
    if m:
        rows = schema.tables.get("ai_tasks")
        ids = [r.get("id") for r in rows.rows] if rows else []
        ids = [v for v in ids if isinstance(v, int)]
        return min(ids) if ids else 0
    try:
        return float(e) if "." in e else int(e)
    except ValueError:
        raise AssertionError(
            f"simulator does not know how to evaluate the backfill expression {expr!r}; "
            "extend the simulator deliberately instead of skipping it"
        ) from None


def apply_script(schema: Schema, statements: list[str] | None = None) -> Schema:
    """Apply the shipped canonical script to `schema`, with PostgreSQL semantics."""
    statements = canonical_statements() if statements is None else statements
    for raw in statements:
        stmt = strip_comments(raw)
        if not stmt:
            continue
        s = _norm(stmt)

        if re.fullmatch(r"(BEGIN|COMMIT)", s, re.I):
            continue
        if re.match(r"^NOTIFY\s", s, re.I):
            continue
        if re.match(r"^CREATE EXTENSION IF NOT EXISTS", s, re.I):
            continue

        m = re.match(r"^CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)$", s, re.S)
        if m:
            name, body = m.group(1), m.group(2)
            if name in schema.tables:
                continue  # silent no-op, exactly like PostgreSQL
            cols: list[tuple[str, str, bool, str | None]] = []
            inline_pk: list[str] = []
            body = re.sub(r"CONSTRAINT\s+\w+\s+", "", body)
            body = _strip_paren_constructs(body, "CHECK")
            composite = re.search(r"PRIMARY KEY \(([^)]*)\)", body)
            body = _strip_paren_constructs(body, "PRIMARY KEY")
            for part in re.split(r",\s*", body):
                part = part.strip()
                if not part or part.lower().startswith("constraint"):
                    continue
                bits = part.split()
                if not bits or not re.fullmatch(r"[a-z_][a-z0-9_]*", bits[0]):
                    continue
                col, rest = bits[0], " ".join(bits[1:])
                type_, _, not_null, default = _split_add_column(rest)
                is_pk = bool(re.search(r"\bPRIMARY KEY\b", rest, re.I))
                if is_pk:
                    inline_pk.append(col)
                cols.append((col, type_, not_null or is_pk, default))
            t = schema.add_table(name, cols)
            t.primary_key = (
                [c.strip() for c in composite.group(1).split(",")]
                if composite
                else inline_pk
            )
            continue

        m = re.match(r"^CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+) (.*)$", s, re.S)
        if m:
            table, rest = m.group(3), m.group(4)
            assert table in schema.tables, f"index on unknown table {table}"
            cols = re.findall(r"\(([^)]*)\)", rest)
            for group in cols:
                for col in group.split(","):
                    col = col.strip().split()[0]
                    if col and re.fullmatch(r"[a-z_][a-z0-9_]*", col):
                        assert schema.contains(table, col), (
                            f"{table}.{col} is indexed but does not exist — the 42703 class"
                        )
            schema.tables[table].indexes.add(m.group(2))
            continue

        m = re.match(r"^ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY$", s, re.I)
        if m:
            assert m.group(1) in schema.tables, f"RLS on unknown table {m.group(1)}"
            continue

        m = re.match(r"^ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) (.*)$", s, re.S)
        if m:
            table, col, rest = m.group(1), m.group(2), m.group(3)
            assert table in schema.tables, (
                f"ADD COLUMN on a table that was never created: {table} "
                "(the canonical script must create it first)"
            )
            t = schema.tables[table]
            if col in t.columns:
                continue
            _, _, not_null, default = _split_add_column(rest)
            t.columns[col] = Column("", not_null, default)
            value = _default_value(default) if default is not None else None
            for row in t.rows:
                row[col] = value
            continue

        m = re.match(r"^ALTER TABLE (\w+) ALTER COLUMN (\w+) SET NOT NULL$", s, re.I)
        if m:
            table, col = m.group(1), m.group(2)
            assert schema.contains(table, col), f"SET NOT NULL on unknown column {table}.{col}"
            t = schema.tables[table]
            bad = [r for r in t.rows if r.get(col) is None]
            assert not bad, (
                f"ALTER TABLE {table} ALTER COLUMN {col} SET NOT NULL fails: "
                f"{len(bad)} row(s) still NULL — the backfill is missing or wrong"
            )
            t.columns[col].not_null = True
            continue

        m = re.match(r"^ALTER TABLE (\w+) ALTER COLUMN (\w+) SET DEFAULT (.+)$", s, re.I | re.S)
        if m:
            table, col, expr = m.group(1), m.group(2), m.group(3).strip()
            assert schema.contains(table, col), f"SET DEFAULT on unknown column {table}.{col}"
            schema.tables[table].columns[col].default = expr
            continue

        m = re.match(r"^ALTER TABLE (\w+) ADD CONSTRAINT (\w+) (.*)$", s, re.S)
        if m:
            table, name, rest = m.group(1), m.group(2), m.group(3)
            assert table in schema.tables, f"ADD CONSTRAINT on unknown table {table}"
            if rest.upper().startswith("CHECK"):
                body, _ = _balanced(rest, rest.index("("))
                _check_identifiers(schema, table, body, f"ALTER TABLE {table}")
            elif rest.upper().startswith("PRIMARY KEY"):
                body, _ = _balanced(rest, rest.index("("))
                schema.tables[table].primary_key = [c.strip() for c in body.split(",")]
            schema.tables[table].indexes.add(name)
            continue

        m = re.match(r"^ALTER TABLE (\w+) DROP CONSTRAINT IF EXISTS (\w+)$", s, re.I)
        if m:
            assert m.group(1) in schema.tables, f"DROP CONSTRAINT on unknown table {m.group(1)}"
            continue

        m = re.match(r"^GRANT SELECT ON (\w+) TO (.+)$", s, re.I)
        if m:
            assert m.group(1) in schema.tables, f"GRANT on unknown table {m.group(1)}"
            continue

        m = re.match(r'^DROP POLICY IF EXISTS "([^"]+)" ON (\w+)$', s, re.I)
        if m:
            assert m.group(2) in schema.tables, f"DROP POLICY on unknown table {m.group(2)}"
            continue

        m = re.match(r'^CREATE POLICY "([^"]+)" ON (\w+) FOR SELECT TO (.+)$', s, re.I | re.S)
        if m:
            assert m.group(2) in schema.tables, f"CREATE POLICY on unknown table {m.group(2)}"
            continue

        m = re.match(r"^INSERT INTO (\w+) \(([^)]*)\) VALUES (.*)$", s, re.S)
        if m:
            table, cols, tail = m.group(1), m.group(2), m.group(3)
            assert table in schema.tables, f"INSERT into unknown table {table}"
            t = schema.tables[table]
            provided = [c.strip() for c in cols.split(",")]
            for col in provided:
                assert schema.contains(table, col), (
                    f"INSERT INTO {table} ({cols}) references {col}, which does not "
                    "exist — this is the reported 42700/42703 failure class"
                )
            for name, col in t.columns.items():
                if col.not_null and col.default is None:
                    assert name in provided, (
                        f"INSERT INTO {table} omits NOT NULL column {name} (no default)"
                    )
            tail = re.sub(r"ON CONFLICT .*$", "", tail, flags=re.S | re.I)
            for tuple_text in re.findall(r"\(([^()]*)\)", tail):
                values = re.findall(r"'((?:[^']|'')*)'|\b(\d+(?:\.\d+)?)\b", tuple_text)
                vals = [a.replace("''", "'") if a else float(b) if "." in b else int(b)
                        for a, b in values]
                if any(re.search(r"\bnow\(\)|\bNULL\b", token, re.I)
                       for token in tuple_text.split(",")):
                    vals = [v if v is not None else "NOW" for v in vals]
                row = dict(zip(provided, vals))
                key = tuple(row.get(k) for k in t.primary_key)
                if t.primary_key and any(
                    tuple(r.get(k) for k in t.primary_key) == key for r in t.rows
                ):
                    continue  # ON CONFLICT DO NOTHING
                schema.insert_row(table, row)
            continue

        m = re.match(r"^UPDATE (\w+) SET (\w+) = (.+?) WHERE \2 IS NULL$", s, re.S | re.I)
        if m:
            table, col, expr = m.group(1), m.group(2), m.group(3).strip()
            assert schema.contains(table, col), f"UPDATE references unknown column {table}.{col}"
            t = schema.tables[table]
            nulls = [i for i, r in enumerate(t.rows) if r.get(col) is None]
            for i in nulls:
                t.rows[i][col] = _resolve(expr, schema, i)
            continue

        m = re.match(r"^DO \$\$(.*)\$\$$", s, re.S)
        if m:
            block = m.group(1)
            for table, name, kind, body in constraint_adds(block):
                assert table in schema.tables, f"DO block constraint on unknown table {table}"
                if kind == "CHECK":
                    _check_identifiers(schema, table, body, f"DO block {name}")
                elif kind == "FOREIGN KEY":
                    for col in body.split(","):
                        col = col.strip()
                        assert schema.contains(table, col), (
                            f"DO block {name}: foreign key on unknown column {table}.{col}"
                        )
                    ref = re.search(r"REFERENCES\s+(\w+)\s*\(([^)]*)\)", block)
                    assert ref and ref.group(1) in schema.tables, (
                        f"DO block {name}: references an unknown table"
                    )
                    for col in ref.group(2).split(","):
                        col = col.strip()
                        assert schema.contains(ref.group(1), col), (
                            f"DO block {name}: references unknown column {ref.group(1)}.{col}"
                        )
            for table, _name in constraint_drops(block):
                assert table in schema.tables, f"DO block drops a constraint on unknown {table}"
            for index_name, table in re.findall(
                r"CREATE (?:UNIQUE )?INDEX (\w+)\s+ON (\w+)", block
            ):
                assert table in schema.tables, f"DO block indexes unknown table {table}"
                schema.tables[table].indexes.add(index_name)
            continue

        if re.match(r"^SELECT ", s, re.I):
            pairs = re.findall(r"\('([a-z_]+)','([a-z_]+)'\)", s)
            assert pairs, "an unexpected bare SELECT statement appeared in the script"
            continue

        raise AssertionError(f"simulator has no handler for this statement: {stmt[:120]!r}")

    return schema


def identity_rows() -> list[tuple[str, str, str, str, str, str]]:
    script = MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"SELECT \* FROM \(VALUES\n(.*?)\n\s*\) AS v\(tbl, cname, kind, cols", script, re.S)
    assert m, "the identity-constraint block must carry a VALUES list"
    rows = re.findall(
        r"\('(\w+)',\s*'(\w+)',\s*'([pu])',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'\)",
        m.group(1),
    )
    assert rows, "identity block VALUES list parsed empty"
    return rows


# ─── fixtures / helpers ──────────────────────────────────────────────────────

def script_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


SETUP_BANNER = "ONE COMPLETE SUPABASE SETUP SCRIPT"


def doc_setup_block() -> str:
    """The ONE complete deployment block (§31.3); it embeds the snapshot as part 1."""
    blocks = sql_blocks(DOC.read_text(encoding="utf-8"))
    target = [b for b in blocks if SETUP_BANNER in b]
    assert len(target) == 1, "DATABASE_ARCHITECTURE.md must embed the setup block exactly once"
    return target[0]


def doc_reconciliation_segment() -> str:
    """Part 1 of the setup block — the canonical snapshot, comment-stripped."""
    block = doc_setup_block()
    start = block.index("-- ─── PART 1 of 5")
    end = block.index("-- ─── PART 2 of 5")
    return block[start:end]


def create_only_statements() -> list[str]:
    return [
        s
        for s in canonical_statements()
        if re.match(r"^CREATE TABLE IF NOT EXISTS ", _norm(strip_comments(s)))
    ]


def fresh_model() -> Schema:
    return apply_script(Schema())


def snapshot(schema: Schema) -> dict[str, tuple]:
    return {
        name: (
            tuple(sorted(t.columns)),
            tuple(sorted(t.indexes)),
            tuple(t.primary_key),
            len(t.rows),
            tuple(sorted((k, str(v)) for k, v in t.rows[0].items())) if t.rows else (),
        )
        for name, t in sorted(schema.tables.items())
    }


def drift(schema: Schema) -> list[str]:
    return [
        f"{tbl}.{col}"
        for tbl, col in drift_report_pairs(script_text())
        if not schema.contains(tbl, col)
    ]


def strip_columns(schema: Schema, table: str, columns: tuple[str, ...]) -> None:
    for col in columns:
        schema.tables[table].columns.pop(col, None)
    schema.tables[table].primary_key = [
        c for c in schema.tables[table].primary_key if c in schema.tables[table].columns
    ]


PANEL_SETTINGS_LATER_COLUMNS = (
    "auto_close_delay", "max_deep_save_mb", "delete_batch_size", "log_retention_days",
    "panel_timeout_seconds", "allow_multiple_panels", "reuse_existing_panel", "language",
    "debug_callbacks", "owner_only", "dashboard_font", "update_stale_seconds",
    "ghost_seen_retention_seconds",
)
AI_CONFIG_LATER_COLUMNS = (
    "trigger_en", "trigger_fa", "show_question", "stt_model", "stt_language", "stt_passes",
)


def legacy_model() -> Schema:
    """The worst realistic pre-repair database: every known drift applied at once."""
    schema = Schema()
    apply_script(schema, create_only_statements())
    strip_columns(schema, "bot_settings", ("value_type",))
    strip_columns(schema, "panel_settings", PANEL_SETTINGS_LATER_COLUMNS)
    strip_columns(schema, "ai_config", AI_CONFIG_LATER_COLUMNS)
    strip_columns(schema, "ai_task_occurrences", ("preparation_metadata",))
    strip_columns(schema, "saved_items", ("short_code", "file_name"))
    return schema


@pytest.fixture()
def bot_settings_legacy_rows() -> list[dict[str, object]]:
    return [
        {"key": "ghost_seen_allowed_chats", "value": "[123, 456]", "updated_at": "T1"},
        {"key": "owner_language", "value": "fa", "updated_at": "T2"},
        {"key": "telemetry_enabled", "value": "false", "updated_at": "T3"},
    ]


# ─── static contracts ────────────────────────────────────────────────────────

def test_the_two_repository_copies_of_the_canonical_script_are_byte_identical():
    migration = script_text().rstrip("\n")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8").rstrip("\n")
    assert bootstrap == migration, "canonical_bootstrap.sql and the migration must be byte-identical"


def test_the_setup_block_embeds_the_canonical_snapshot_statement_for_statement():
    embedded = [strip_comments(s) for s in split_statements(doc_reconciliation_segment())]
    migration = [strip_comments(s) for s in canonical_statements()]
    assert embedded, "part 1 of the §31.3 block must not be empty"
    assert embedded == migration, (
        "part 1 of the §31.3 deployment block must carry every canonical statement, in order"
    )


def test_the_migration_is_forward_only_and_its_successors_stay_additive():
    assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", MIGRATION.name), MIGRATION.name
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert MIGRATION.name in names, "the repair must stay in supabase/migrations/"
    others = [p for p in MIGRATIONS_DIR.glob("*.sql") if p.name != MIGRATION.name]
    for path in others:
        assert "schema-drift" not in path.read_text(encoding="utf-8") or "reconcile" not in path.name
    assert "reconcile_canonical_schema" not in "".join(
        p.read_text(encoding="utf-8") for p in others
    ), "historical migrations must not be rewritten to point at the repair"
    # This script is the reconciliation SNAPSHOT: a later schema change arrives
    # as its own additive file rather than by editing the repair or a historical
    # migration. Every file newer than it must therefore be additive-only — the
    # rollback text in a header comment is documentation, not an executed
    # statement, so only the executable statements are inspected.
    for path in [p for p in others if p.name > MIGRATION.name]:
        executable = strip_comments(path.read_text(encoding="utf-8"))
        for pattern, verb in (
            (r"\bDROP\s+TABLE\b", "drops a table"),
            (r"\bTRUNCATE\b", "truncates"),
            (r"\bDELETE\s+FROM\b", "deletes rows"),
            (r"\bDROP\s+COLUMN\b", "drops a column"),
        ):
            assert not re.search(pattern, executable, re.I), (
                f"{path.name} {verb} — a forward-only successor must be additive"
            )


def test_the_canonical_contract_is_the_documented_sixteen_tables():
    declared = declared_column_sets(script_text())
    assert set(declared) == set(CANONICAL_TABLES)
    assert len(CANONICAL_TABLES) == 16
    for t in NON_CANONICAL_TABLES:
        assert t not in declared, f"{t} must stay non-canonical (documented, not migrated)"
        assert t in DOC.read_text(encoding="utf-8"), f"{t} must still be documented"


def test_every_table_declares_adds_and_verifies_exactly_the_same_columns():
    script = script_text()
    declared = declared_column_sets(script)
    added = added_column_sets(script)
    verified: dict[str, list[str]] = {}
    for tbl, col in drift_report_pairs(script):
        verified.setdefault(tbl, []).append(col)
    for table in CANONICAL_TABLES:
        d, a, v = declared[table], added.get(table, []), verified.get(table, [])
        assert len(d) == len(set(d)), f"{table} declares a duplicate column"
        assert len(a) == len(set(a)), f"{table} adds a duplicate column"
        assert set(d) == set(a), (
            f"{table}: columns only in CREATE {sorted(set(d) - set(a))}, "
            f"only in ADD COLUMN {sorted(set(a) - set(d))}"
        )
        assert set(d) == set(v), (
            f"{table}: drift report is out of sync — only in CREATE "
            f"{sorted(set(d) - set(v))}, only in the report {sorted(set(v) - set(d))}"
        )


def test_no_canonical_column_is_established_by_create_table_alone():
    """Every canonical column must be re-asserted with ADD COLUMN IF NOT EXISTS."""
    script = script_text()
    declared = declared_column_sets(script)
    added = added_column_sets(script)
    missing = [f"{t}.{c}" for t, cols in declared.items() for c in cols if c not in added[t]]
    assert missing == [], f"columns that a legacy table could never gain: {missing}"


def test_every_canonical_table_has_an_identity_constraint_entry():
    rows = identity_rows()
    covered = {r[0] for r in rows}
    assert covered == set(CANONICAL_TABLES), (
        f"identity block missing {sorted(set(CANONICAL_TABLES) - covered)}"
    )
    for table, _name, kind, cols, nullexpr, distinctexpr in rows:
        assert kind in ("p", "u")
        assert cols.strip()
        assert nullexpr.strip() and distinctexpr.strip()


def test_identity_constraints_target_columns_that_exist_after_reconciliation():
    schema = fresh_model()
    for table, _name, _kind, cols, _nullexpr, _distinctexpr in identity_rows():
        for col in cols.split(","):
            col = col.strip()
            assert schema.contains(table, col), f"{table}.{col} is not a canonical column"


def test_the_script_is_additive_only():
    script = script_text()
    code = "\n".join(
        line for line in script.split("\n") if not line.strip().startswith("--")
    )
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "DROP SCHEMA"):
        assert forbidden not in code, f"the reconciliation script must not contain {forbidden}"
    assert "DROP CONSTRAINT IF EXISTS" in code, "constraint replacement must stay explicit"
    assert "DROP POLICY IF EXISTS" in code, "stale anon write policies must still be dropped"


def test_the_script_never_touches_the_credential_vault_of_section_29():
    # Comments may *name* the §29 objects to say they are excluded; executable
    # statements must never reference them.
    code = "\n".join(
        line for line in script_text().split("\n") if not line.strip().startswith("--")
    )
    for forbidden in ("api_credentials", "api_credential_pool", "vault.secrets", "vault.create_secret"):
        assert forbidden not in code, f"the reconciliation script must not own {forbidden}"
    section = section_30()
    assert "§29" in section, "§30 must point at the credential-vault section"


def test_every_data_dependent_constraint_addition_is_guarded_by_a_warning():
    script = script_text()
    guarded = re.findall(
        r"RAISE WARNING '((?:panel_settings|ai_config|ai_sessions|ai_messages|ai_memories|"
        r"ai_tasks|ai_task_occurrences|ai_task_occurrences_task_id_fkey)[^']*)'",
        script,
    )
    names = {g.split()[0].rstrip(":") for g in guarded}
    for expected in (
        "panel_settings_auto_close_delay_check",
        "panel_settings_max_deep_save_mb_check",
        "panel_settings_delete_batch_size_check",
        "panel_settings_log_retention_days_check",
        "panel_settings_panel_timeout_seconds_check",
        "panel_settings_language_check",
        "panel_settings_dashboard_font_check",
        "panel_settings_ghost_seen_retention_seconds_check",
        "ai_config_stt_passes_range",
        "ai_sessions_status_check",
        "ai_messages_role_check",
        "ai_memories_tier_check",
        "ai_memories_category_check",
        "ai_tasks_label_not_blank",
        "ai_tasks_status_check",
        "ai_tasks_version_check",
        "ai_tasks_schedule_type_check",
        "ai_tasks_actions_check",
        "ai_tasks_actions_count",
        "ai_tasks_payload_size",
        "ai_tasks_schedule_size",
        "ai_tasks_destination_size",
        "ai_tasks_ai_instruction_size",
        "ai_task_occurrences_key_not_blank",
        "ai_task_occurrences_definition_version_check",
        "ai_task_occurrences_action_count",
        "ai_task_occurrences_payload_size",
        "ai_task_occurrences_attempt_check",
        "ai_task_occurrences_status_check",
        "ai_task_occurrences_error_metadata_check",
        "ai_task_occurrences_retry_state",
        "ai_task_occurrences_preparation_metadata_object",
        "ai_task_occurrences_error_size",
        "ai_task_occurrences_result_size",
        "ai_task_occurrences_preparation_size",
        "ai_task_occurrences_task_id_fkey",
    ):
        assert expected in names, f"{expected} is applied without a data guard"

    for unique_index in ("idx_saved_items_short_code", "uq_ai_task_occurrences_task_key"):
        assert re.search(
            rf"RAISE WARNING '[^']*{unique_index}", script
        ), f"{unique_index} is created without a duplicate guard"
        assert "duplicate" in script


def test_the_migration_reloads_the_postgrest_schema_cache():
    assert "NOTIFY pgrst, 'reload schema'" in script_text()


# ─── simulated execution ─────────────────────────────────────────────────────

def test_a_fresh_database_reaches_the_full_canonical_contract():
    schema = fresh_model()
    assert set(schema.tables) == set(CANONICAL_TABLES)
    declared = declared_column_sets(script_text())
    for table, columns in declared.items():
        assert schema.tables[table].column_names == columns
    assert drift(schema) == []


def test_every_canonical_table_has_a_primary_key_after_reconciliation():
    schema = fresh_model()
    for table, t in schema.tables.items():
        assert t.primary_key, f"{table} has no primary key in the canonical contract"


def test_reconciliation_seeds_the_two_required_rows():
    schema = fresh_model()
    assert [r["key"] for r in schema.tables["panel_settings"].rows] == ["global"]
    assert sorted(r["key"] for r in schema.tables["bot_settings"].rows) == [
        "auto_close_enabled", "delete_batch_size", "log_cleanup_days",
        "max_deep_save_mb", "panel_auto_close_seconds",
    ]
    assert "ghost_seen_allowed_chats" not in [
        r["key"] for r in schema.tables["bot_settings"].rows
    ], "the Ghost Seen allow-list row must be created at runtime, never seeded"


def test_the_seeds_use_a_targetless_on_conflict():
    script = script_text()
    assert script.count("ON CONFLICT DO NOTHING") >= 2
    assert "ON CONFLICT (key) DO NOTHING" not in script, (
        "a targeted ON CONFLICT would fail on a legacy table without the unique index"
    )


def test_the_reported_production_failure_is_reproducible_and_fixed(bot_settings_legacy_rows):
    """The exact 42703 class: bot_settings existing WITHOUT value_type."""
    legacy = Schema()
    apply_script(legacy, create_only_statements())
    strip_columns(legacy, "bot_settings", ("value_type",))

    for row in bot_settings_legacy_rows:
        legacy.insert_row("bot_settings", row)
    assert "value_type" not in legacy.tables["bot_settings"].columns

    # 1. The old pattern (CREATE IF NOT EXISTS then INSERT) still fails.
    old_statements = [
        "CREATE TABLE IF NOT EXISTS bot_settings (\n"
        "    key text PRIMARY KEY,\n"
        "    value text NOT NULL,\n"
        "    value_type text NOT NULL DEFAULT 'str',\n"
        "    updated_at timestamptz DEFAULT now()\n"
        ")",
        "INSERT INTO bot_settings (key, value, value_type) VALUES ('auto_close_enabled', 'true', 'bool') "
        "ON CONFLICT DO NOTHING",
    ]
    with pytest.raises(AssertionError) as excinfo:
        apply_script(legacy, old_statements)
    assert "value_type" in str(excinfo.value)

    # 2. The shipped reconciliation script does not.
    schema = legacy_model()
    for row in bot_settings_legacy_rows:
        schema.insert_row("bot_settings", row)
    apply_script(schema)

    table = schema.tables["bot_settings"]
    assert "value_type" in table.columns
    assert table.columns["value_type"].not_null is True
    assert table.columns["value_type"].default == "'str'"

    by_key = {r["key"]: r for r in table.rows}
    for row in bot_settings_legacy_rows:
        assert by_key[row["key"]]["value"] == row["value"], "existing rows must be preserved"
        assert by_key[row["key"]]["value_type"] == "str", "the new column must be backfilled"
    assert len(by_key) == len(bot_settings_legacy_rows) + 5
    assert drift(schema) == []


def test_legacy_panel_settings_three_column_shape_converges():
    schema = legacy_model()
    before = set(schema.tables["panel_settings"].columns)
    assert before == {"key", "auto_close_enabled", "updated_at"}, before
    apply_script(schema)
    after = set(schema.tables["panel_settings"].columns)
    assert after == set(declared_column_sets(script_text())["panel_settings"])
    assert set(PANEL_SETTINGS_LATER_COLUMNS) <= after


def test_legacy_ai_config_gains_the_columns_every_upsert_writes():
    schema = legacy_model()
    assert "show_question" not in schema.tables["ai_config"].columns
    schema.insert_row("ai_config", {"owner_id": 7283627550, "provider": "gemini", "model": "flash"})
    apply_script(schema)
    cols = schema.tables["ai_config"].columns
    for col in AI_CONFIG_LATER_COLUMNS:
        assert col in cols, f"ai_config.{col} must be part of the canonical contract"
    row = schema.tables["ai_config"].rows[0]
    assert row["provider"] == "gemini" and row["model"] == "flash"
    assert row["show_question"] is False
    assert row["stt_passes"] == 1


def test_legacy_ai_task_occurrences_gains_preparation_metadata():
    schema = legacy_model()
    assert "preparation_metadata" not in schema.tables["ai_task_occurrences"].columns
    apply_script(schema)
    col = schema.tables["ai_task_occurrences"].columns["preparation_metadata"]
    assert col.not_null is True
    assert col.default == "'{}'"


def test_legacy_saved_items_gains_the_columns_its_indexes_need():
    schema = legacy_model()
    assert "short_code" not in schema.tables["saved_items"].columns
    apply_script(schema)
    cols = schema.tables["saved_items"].columns
    assert {"short_code", "file_name"} <= set(cols)
    assert "idx_saved_items_short_code" in schema.tables["saved_items"].indexes


def test_the_worst_case_legacy_database_converges_and_reports_no_drift():
    schema = legacy_model()
    missing_before = drift(schema)
    assert "bot_settings.value_type" in missing_before
    assert "panel_settings.auto_close_delay" in missing_before
    assert "ai_config.show_question" in missing_before
    assert "ai_task_occurrences.preparation_metadata" in missing_before
    assert "saved_items.short_code" in missing_before

    apply_script(schema)

    assert drift(schema) == [], "the drift report must come back empty after reconciliation"
    declared = declared_column_sets(script_text())
    for table, columns in declared.items():
        # A legacy table re-gains its columns at the end, so compare as sets.
        assert set(schema.tables[table].column_names) == set(columns), table


def test_applying_the_script_twice_changes_nothing():
    schema = fresh_model()
    first = snapshot(schema)
    apply_script(schema)
    apply_script(schema)
    assert snapshot(schema) == first


def test_applying_twice_to_a_legacy_database_changes_nothing_the_second_time():
    schema = legacy_model()
    apply_script(schema)
    first = snapshot(schema)
    apply_script(schema)
    assert snapshot(schema) == first
    assert drift(schema) == []


def test_applying_to_an_already_canonical_database_is_a_no_op():
    schema = fresh_model()
    rows_before = len(schema.tables["bot_settings"].rows)
    apply_script(schema)
    assert len(schema.tables["bot_settings"].rows) == rows_before


def test_the_drift_report_is_evaluated_after_the_commit():
    script = script_text()
    commit = script.index("COMMIT;")
    report = script.index("missing_canonical_column")
    assert report > commit, "the drift report must run after COMMIT so it survives"
    assert script.index("BEGIN;") < commit


# ─── documentation contracts ─────────────────────────────────────────────────

def test_the_documentation_records_the_root_cause_and_the_exact_error():
    section = section_30()
    assert "42703" in section
    assert 'column "value_type" of relation "bot_settings" does not exist' in section
    assert "CREATE TABLE IF NOT EXISTS" in section
    assert "silent NO-OP" in section or "silent no-op" in section


def test_the_documentation_audits_every_instance_of_the_class():
    section = section_30()
    for object_name in ("bot_settings", "panel_settings", "saved_items", "ai_config",
                        "ai_task_occurrences", "ai_tasks"):
        assert object_name in section
    assert "ai_preferences" in section, "the deliberately non-canonical table must be named"
    assert "tool_calls" in section, "the deliberately excluded column must be named"


def test_the_documentation_states_that_supabase_was_not_touched():
    doc = DOC.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", section_30())
    assert "NOT EXECUTED BY AI" in flat
    assert "NOT APPLIED" in flat
    assert "Live Supabase state was NOT inspected" in doc
    assert "NOT been executed against any" in doc


def test_the_documentation_carries_a_rollback_block_and_names_irreversibility():
    sql = "\n".join(sql_blocks(section_30())).lower()
    assert "rollback" in section_30().lower()
    assert "drop function" in sql or "drop column" in sql
    assert "irreversib" in section_30().lower(), (
        "§30 must state explicitly that the additive repair has no safe rollback"
    )


def test_the_documentation_separates_the_optional_destructive_cleanup():
    section = section_30()
    assert "OPTIONAL" in section.upper()
    assert "DESTRUCTIVE" in section.upper()
    assert "§20" in section, "the cleanup must point at the recorded §20 proposals"


def test_no_migration_creates_a_table_that_the_documentation_does_not_classify():
    created: set[str] = set()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        created |= set(declared_column_sets(path.read_text(encoding="utf-8")))
    known = set(CANONICAL_TABLES) | set(NON_CANONICAL_TABLES)
    assert created <= known, f"undocumented tables created by migrations: {sorted(created - known)}"


def test_the_reconciliation_script_keeps_the_documented_security_model():
    script = script_text()
    assert script.count("ENABLE ROW LEVEL SECURITY") >= len(CANONICAL_TABLES)
    for forbidden in ("FOR ALL", "TO PUBLIC", "DISABLE ROW LEVEL SECURITY"):
        assert forbidden not in script, f"{forbidden} would contradict the documented RLS model"
    anon_writes = re.findall(r'CREATE POLICY "[^"]*" ON \w+ FOR (INSERT|UPDATE|DELETE)', script)
    assert anon_writes == [], f"anon must never get a write policy: {anon_writes}"
