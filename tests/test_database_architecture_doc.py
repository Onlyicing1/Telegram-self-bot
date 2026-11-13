"""Source-to-document contract for DATABASE_ARCHITECTURE.md.

These tests protect the *inventory* properties an audit must hold, not the
document's wording:

* every migration file in ``supabase/migrations/`` is classified in §31;
* the declared migration count equals the repository's;
* every table the migrations create is listed in the §1 overview and the count
  there equals the real one;
* the Appearance migration is embedded as part 15 and its five columns are in
  the documented §6 schema with the real type/nullability/default;
* the documented internal-setting accessor count equals the settings service's
  own key set (no stale "14 typed settings" claim);
* the dedicated §35 Appearance chapter states the bucket environment variable,
  prefix, bounds and "no gallery table" model that the source implements;
* the §25 persistence inventory names the current features.

They read only repository files (and pure constants from the services), so they
need no database and no network.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
DOC = DOC_PATH.read_text(encoding="utf-8")
MIGRATIONS_DIR = REPO / "supabase" / "migrations"
MIGRATIONS = sorted(MIGRATIONS_DIR.glob("*.sql"))

APPEARANCE_COLUMNS = (
    ("appearance_header_title", "text", "''"),
    ("appearance_header_body", "text", "''"),
    ("appearance_page_overrides", "jsonb", "'{}'"),
    ("appearance_preview_enabled", "boolean", "false"),
    ("appearance_preview_image_key", "text", "''"),
)


def section(marker: str) -> str:
    start = DOC.index(marker)
    nxt = DOC.find("\n## ", start + len(marker))
    return DOC[start:] if nxt == -1 else DOC[start:nxt]


def migrated_tables() -> set[str]:
    """Every table the migration files actually create (comments excluded)."""
    found: set[str] = set()
    for path in MIGRATIONS:
        sql = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"^\s*CREATE TABLE IF NOT EXISTS\s+(?:public\.)?([a-z_]+)",
            sql,
            flags=re.MULTILINE,
        ):
            found.add(match.group(1))
    return found


# ─── Migrations ──────────────────────────────────────────────────────────────

def test_every_migration_file_is_classified_in_the_audit_section():
    """§31 is the authoritative inventory: no migration may be omitted."""
    audit = section("## 31. Canonical Database Setup")
    missing = [path.name for path in MIGRATIONS if path.name[:14] not in audit]
    assert len(MIGRATIONS) == 42, "the repository's migration count changed"
    assert missing == [], f"migrations absent from §31: {missing}"
    assert "all 41 migrations" in audit


def test_migration_status_states_the_same_count():
    status = section("## 22. Migration Status")
    flat = " ".join(status.split())
    assert "**42** files" in flat
    assert "all **36** files" not in flat


def test_the_appearance_migration_is_part_fifteen():
    audit = section("## 31. Canonical Database Setup")
    assert (
        "PART 15 of 18 — 20261109000000_add_panel_settings_appearance.sql"
        in audit
    )
    assert (
        "PART 16 of 18 — 20261112000000_add_panel_settings_outbound_bandwidth.sql"
        in audit
    )
    part = audit[audit.index("PART 15 of 18"): audit.index("PART 16 of 18")]
    for column, _type, _default in APPEARANCE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in part


# ─── Tables ──────────────────────────────────────────────────────────────────

def test_documented_table_count_equals_the_migrations():
    tables = migrated_tables()
    assert len(tables) == 28, f"migration table set changed: {sorted(tables)}"
    overview = section("## 1. Overview")
    assert "**28 tables**" in overview
    absent = [table for table in tables if f"`{table}`" not in overview]
    assert absent == [], f"tables absent from the §1 overview: {absent}"
    assert "`ai_preferences`" in overview
    assert "no migration and no producer" in overview


def test_the_hermes_boundary_uses_the_real_table_count():
    boundary = section("## 26. Hermes Integration Boundary")
    assert "**28 tables**" in boundary
    assert "13 live/specified tables" not in boundary
    # It must keep its honest scope statement about the unavailable source.
    assert "NOT inspected" in boundary


# ─── panel_settings and Appearance ───────────────────────────────────────────

def test_section_6_documents_every_appearance_column_with_its_real_shape():
    panel = section("## 6. panel_settings")
    for column, sql_type, default in APPEARANCE_COLUMNS:
        assert f"| `{column}` | `{sql_type}` | NO | `{default}` |" in panel


def test_section_6_accessor_count_matches_the_settings_service():
    from backend.services.settings_service import _DEFAULTS

    panel = section("## 6. panel_settings")
    assert len(_DEFAULTS) == 21
    assert "**21 typed getters + 21 typed setters**" in panel
    assert "14 typed getters" not in panel
    for column, _type, _default in APPEARANCE_COLUMNS:
        assert column in _DEFAULTS, f"{column} is not a settings key"
        assert column in panel


def test_the_appearance_migration_and_its_checks_match_the_documented_columns():
    source = (
        MIGRATIONS_DIR / "20261109000000_add_panel_settings_appearance.sql"
    ).read_text(encoding="utf-8")
    panel = section("## 6. panel_settings")
    for column, sql_type, default in APPEARANCE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} {sql_type};" in source
        # jsonb binds the value with a cast; the text/default contract is the same.
        assert f"ALTER COLUMN {column} SET DEFAULT {default}" in source
        assert column in panel
    # The documented constraints are the migration's constraints.
    assert "length(appearance_header_title) <= 1024" in source
    assert "jsonb_typeof(appearance_page_overrides) = 'object'" in source
    assert "position(chr(0) in appearance_header_title) = 0" in source
    chapter = section("## 35. Appearance Persistence")
    assert "jsonb_typeof(appearance_page_overrides) = 'object'" in chapter


def test_appearance_chapter_matches_the_source_storage_model():
    from backend.services import appearance_service as svc
    from backend.services.settings_service import _STORAGE_KEY_RE

    chapter = section("## 35. Appearance Persistence")
    assert 'os.getenv("APPEARANCE_MEDIA_BUCKET"' in chapter
    assert svc.IMAGES_PREFIX in chapter
    assert f"{svc.MAX_GALLERY_IMAGES} images" in chapter
    assert f"{svc.MAX_IMAGE_MB} MiB" in chapter
    assert "26_214_400" in chapter
    assert str(svc.MAX_IMAGE_BYTES) == "26214400"
    assert str(svc.MAX_IMAGE_SIDE) in chapter
    assert f"{svc.MAX_IMAGE_PIXELS // 1_000_000} megapixels" in chapter
    # Key-only persistence, gallery from Storage, no gallery table.
    assert "appearance_preview_image_key" in chapter
    assert "never stores image bytes" in chapter
    assert "Storage listing" in chapter
    assert "no gallery table" in chapter
    # The DB constraint and the service validator are the same shape.
    assert _STORAGE_KEY_RE.pattern in chapter
    # Access model unchanged, and delivery state is process-local.
    assert "RLS" in chapter and "SELECT-only" in chapter
    assert "process-local" in chapter
    assert "rollback" in chapter.lower()


# ─── Persistence inventory ───────────────────────────────────────────────────

def test_persistence_inventory_covers_the_current_feature_set():
    inventory = section("## 25. Self Bot Persistent State Inventory")
    for feature in (
        "Appearance header",
        "Appearance image gallery",
        "appearance_preview_image_key",
        "Appearance delivery state",
        "Auto-Reaction",
        "Emoji & Reaction",
        "Private Message management",
        "Anti-Edit / Anti-Delete",
        "AI task scheduler",
        "Todo steps",
        "Test Modules snapshot",
        "API credential vault",
    ):
        assert feature in inventory, f"inventory omits {feature!r}"
    assert "12 typed settings" not in inventory
    assert "tables not migrated" not in inventory


def test_the_current_state_sections_do_not_call_migrated_tables_missing():
    """§19 keeps the labelled history; the current-state sections must not."""
    for marker in ("## 6. panel_settings", "## 22. Migration Status",
                   "## 25. Self Bot Persistent State Inventory",
                   "## 26. Hermes Integration Boundary"):
        body = section(marker)
        assert "10 columns missing" not in body, marker
        assert "trigger columns missing" not in body, marker
        assert "tables not migrated" not in body, marker


def test_the_outbound_bandwidth_migration_is_part_sixteen():
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "DATABASE_ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    marker = "PART 16 of 18 — 20261112000000_add_panel_settings_outbound_bandwidth.sql"
    assert marker in doc
    part = doc[doc.index(marker):]
    assert "outbound_bandwidth_state" in part
    assert "jsonb_typeof(outbound_bandwidth_state) = 'object'" in part
    assert part.count("ALTER TABLE panel_settings") >= 4
