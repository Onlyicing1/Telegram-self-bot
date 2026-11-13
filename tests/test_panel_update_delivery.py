"""Panel repaint vs. media-delivery identity — ordinary updates keep ONE message.

The regression: once the Appearance preview became the panel's MEDIA, every
repaint asked ``needs_new_message(...)`` with the PAINT's own text as an input.
A save result, a model test's progress line or any status whose text could not
be a caption flipped the answer to "text", so the panel message was REPLACED —
a new inline result created and the old message deleted — in the middle of the
operation that was still editing it.

These tests drive the real dispatch, lifecycle and paint paths
(``panels.dispatch_click`` → ``_handle_action`` / ``_handle_panel`` →
``_render_and_edit`` → ``_replace_for_delivery`` → ``PanelLifecycleManager``)
and assert on MESSAGE IDENTITY: which message the panel stays on, whether a new
inline panel was created, and whether the old one was deleted.
"""
from __future__ import annotations

import struct
import zlib

import pytest

from backend.helper import callback_gate
from backend.helper import inline_engine
from backend.helper import lifecycle as lifecycle_mod
from backend.helper import panel_registry
from backend.helper import panels
from backend.helper.panel_render import render_edit
from backend.services import (
    appearance_service,
    panel_settings_repository,
    settings_service,
)

CHAT = -1001234567890
MSG = 700
NEW_MSG = 701
OWNER = 4242


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    settings_service._cache = dict(settings_service._DEFAULTS)
    settings_service._cache["appearance_page_overrides"] = {}
    settings_service._loaded = True
    appearance_service.reset_delivery_state()
    for name in (
        "PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
        "APPEARANCE_MEDIA_BUCKET",
        "ARCHIVE_MEDIA_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    row: dict = {}

    def _update(field, value):
        row[field] = value
        return True

    monkeypatch.setattr(panel_settings_repository, "update_field", _update)
    monkeypatch.setattr(
        panel_settings_repository, "load", lambda: dict(row) if row else None
    )
    yield
    appearance_service.reset_delivery_state()


class _Bucket:
    def __init__(self):
        self.objects: dict = {}
        self.downloads = 0
        self.uploads = 0

    def upload(self, path, file, options=None):
        self.uploads += 1
        self.objects[path] = bytes(file)

    def remove(self, paths):
        for path in paths:
            self.objects.pop(path, None)

    def download(self, path):
        self.downloads += 1
        return self.objects[path]

    def list(self, path=None, options=None):
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries = []
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            name = key[len(prefix):]
            if "/" in name:
                continue
            entries.append({
                "name": name,
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {"size": len(self.objects[key])},
            })
        return entries


class _Storage:
    def __init__(self, bucket):
        self.bucket = bucket

    def from_(self, _bucket):
        return self.bucket


class _DB:
    def __init__(self, bucket):
        self.storage = _Storage(bucket)


@pytest.fixture
def store(monkeypatch):
    from backend.db import client as db_client

    bucket = _Bucket()
    monkeypatch.setattr(db_client, "get_db", lambda: _DB(bucket))
    monkeypatch.setenv("APPEARANCE_MEDIA_BUCKET", "appearance-assets")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    return bucket


def png_bytes(width: int = 8, height: int = 6, colour=(10, 120, 200)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _install(store, width: int = 8, height: int = 6) -> str:
    ok, notice = await appearance_service.install_image(png_bytes(width, height))
    assert ok is True, notice
    assert settings_service.set_appearance_preview_enabled(True) is True
    appearance_service.reset_delivery_state()
    return appearance_service.preview_version()


class _SelfClient:
    def __init__(self):
        self.deleted: list = []

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))

    async def edit_message(self, *args, **kwargs):  # pragma: no cover - not the path
        return None


class _Event:
    """The callback event a click carries: its message IS the panel."""

    def __init__(self):
        self.chat_id = CHAT
        self.message_id = MSG
        self.sender_id = OWNER
        self.edits: list = []
        self.answers = 0

    async def answer(self, *args, **kwargs):
        self.answers += 1

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return True


@pytest.fixture
def panel(monkeypatch, store):
    manager = lifecycle_mod.PanelLifecycleManager()
    client = _SelfClient()
    manager.configure(client, OWNER)
    manager.sessions.create(CHAT, MSG, panel_type="menu", extra="")
    monkeypatch.setattr(panels, "get_lifecycle", lambda: manager)
    callback_gate.forget_painter(CHAT, MSG)
    yield manager, client
    callback_gate.forget_painter(CHAT, MSG)


@pytest.fixture
def inline_calls(monkeypatch):
    """Record every inline panel creation (i.e. every replacement)."""
    calls: list = []

    async def _fake(self_client, chat_id, query):
        calls.append((chat_id, query))
        return (True, CHAT, NEW_MSG, "")

    monkeypatch.setattr(inline_engine, "trigger", _fake)
    return calls


def _action(monkeypatch, action_id, handler):
    monkeypatch.setitem(panels._actions, action_id, handler)


def _panel(monkeypatch, panel_id, handler, parent: str = "menu"):
    monkeypatch.setitem(
        panel_registry.registry()._panels,
        panel_id,
        panel_registry.PanelDef(
            panel_id=panel_id, render_function=handler, parent_panel=parent
        ),
    )


def _data(rows) -> list[str]:
    out: list[str] = []
    for row in rows or []:
        for button in getattr(row, "buttons", row):
            value = getattr(button, "data", None)
            out.append(value.decode() if isinstance(value, bytes) else str(value))
    return out


async def _save_result(event, extra, chat_id):
    return ("Save", "Saved S0042 — Deep Save completed (Photo, 1.2 MB).", [])


# ── ordinary operational updates: same message, no new panel ───────────────

@pytest.mark.asyncio
async def test_an_operational_update_never_replaces_the_photo_panel(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))
    _action(monkeypatch, "_t_save_result", _save_result)

    event = _Event()
    await panels.dispatch_click(event, "action:_t_save_result", CHAT, MSG, OWNER)

    assert inline_calls == []  # no second inline panel was built
    assert client.deleted == []  # the message the operation edits still exists
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.get(CHAT, NEW_MSG) is None
    assert len(manager.sessions.all_keys()) == 1
    assert "S0042" in event.edits[-1][0]
    # The panel's own keyboard (Close on a root view) is still the one painted.
    assert "panel:_nav:close" in _data(event.edits[-1][1]["buttons"])


@pytest.mark.asyncio
async def test_a_save_result_does_not_rebuild_the_root_menu(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))
    manager.sessions.push_nav(CHAT, MSG, "save", "")
    _action(monkeypatch, "_t_save_result", _save_result)

    event = _Event()
    await panels.dispatch_click(event, "action:_t_save_result", CHAT, MSG, OWNER)

    assert inline_calls == []
    assert client.deleted == []
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.current_nav(CHAT, MSG)[0] == "save"
    assert "Saved S0042" in event.edits[-1][0]
    # A sub-panel gets Back/Home/Close while it stays the same message.
    assert "panel:_nav:back" in _data(event.edits[-1][1]["buttons"])
    assert "Choose a category:" not in event.edits[-1][0]


@pytest.mark.asyncio
async def test_model_tester_progress_updates_keep_one_panel_message(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))
    manager.sessions.reset_nav(CHAT, MSG, "ai", "")

    async def _launch(event, extra, chat_id):
        return ("Test Modules", "▱▱▱▱▱  0/8", [])

    _action(monkeypatch, "_t_test_launch", _launch)
    event = _Event()
    await panels.dispatch_click(event, "action:_t_test_launch", CHAT, MSG, OWNER)

    for done in range(1, 4):
        text, buttons = render_edit("Test Modules", f"▰▱▱▱▱  {done}/8", [])
        await panels._edit_panel_message(event, CHAT, MSG, text, buttons)

    assert inline_calls == []
    assert client.deleted == []
    assert manager.sessions.get(CHAT, MSG) is not None
    assert len(manager.sessions.all_keys()) == 1
    assert len(event.edits) >= 4
    assert all("Test Modules" in text for text, _kwargs in event.edits[-4:])


@pytest.mark.asyncio
async def test_multiple_consecutive_updates_preserve_the_panel_message(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))

    async def _status(event, extra, chat_id):
        return ("Save", f"Progress {extra}", [])

    _action(monkeypatch, "_t_status", _status)
    event = _Event()
    for step in ("1", "2", "3", "4"):
        await panels.dispatch_click(event, f"action:_t_status:{step}", CHAT, MSG, OWNER)

    assert inline_calls == []
    assert client.deleted == []
    assert len(manager.sessions.all_keys()) == 1
    assert manager.sessions.get(CHAT, MSG) is not None
    seen = [text for text, _kwargs in event.edits]
    for step in ("1", "2", "3", "4"):
        assert any(f"Progress {step}" in text for text in seen)


# ── navigation: the image stays attached ───────────────────────────────────

@pytest.mark.asyncio
async def test_normal_category_navigation_keeps_the_image_attached(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))

    async def _save_panel(event, extra):
        return (
            "Save",
            "Deep Save downloads the message and re-uploads it as a new Saved "
            "Messages message.",
            [],
        )

    _panel(monkeypatch, "save", _save_panel)

    event = _Event()
    await panels.dispatch_click(event, "panel:save", CHAT, MSG, OWNER)

    assert inline_calls == []
    assert client.deleted == []
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.current_nav(CHAT, MSG)[0] == "save"
    # The message is still the SAME media delivery: nothing was swapped.
    assert appearance_service.delivered_state(CHAT, MSG) == ("media", version)
    assert "Deep Save" in event.edits[-1][0]


@pytest.mark.asyncio
async def test_a_sub_view_is_not_replaced(store, panel, inline_calls, monkeypatch):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))

    async def _save_panel(event, extra):
        return ("Save", "Reply to the message you want to save.", [])

    _panel(monkeypatch, "save", _save_panel)

    event = _Event()
    await panels.dispatch_click(event, "panel:save:type:d", CHAT, MSG, OWNER)

    assert inline_calls == []
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.current_nav(CHAT, MSG) == ("save", "type:d")
    assert "Reply to the message" in event.edits[-1][0]


# ── genuine delivery transitions still replace ─────────────────────────────

@pytest.mark.asyncio
async def test_a_new_selected_image_version_replaces_the_panel(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    first = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", first))
    assert await appearance_service.delivery_ready() is True

    ok, notice = await appearance_service.install_image(png_bytes(9, 7))
    assert ok is True, notice
    second = appearance_service.preview_version()
    assert second != first
    # What the replacement's own inline build produces for the new selection.
    appearance_service.note_built("menu", ("media", second))

    async def _menu(event, extra):
        return ("LifeOS", "Choose a category:", [])

    _panel(monkeypatch, "menu", _menu)

    event = _Event()
    await panels.dispatch_click(event, "panel:menu", CHAT, MSG, OWNER)

    assert inline_calls == [(CHAT, "menu")]
    assert client.deleted == [(CHAT, [MSG])]
    assert manager.sessions.get(CHAT, NEW_MSG) is not None
    assert manager.sessions.get(CHAT, MSG) is None
    assert appearance_service.delivered_state(CHAT, NEW_MSG) == ("media", second)
    assert event.edits == []  # the old message is never painted after a replace


@pytest.mark.asyncio
async def test_disabling_preview_transitions_the_panel_to_text(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))
    assert settings_service.set_appearance_preview_enabled(False) is True
    appearance_service.note_built("menu", ("text", ""))

    async def _menu(event, extra):
        return ("LifeOS", "Choose a category:", [])

    _panel(monkeypatch, "menu", _menu)

    event = _Event()
    await panels.dispatch_click(event, "panel:menu", CHAT, MSG, OWNER)

    assert inline_calls == [(CHAT, "menu")]
    assert manager.sessions.get(CHAT, NEW_MSG) is not None
    assert manager.sessions.get(CHAT, MSG) is None
    assert appearance_service.delivered_state(CHAT, NEW_MSG) == ("text", "")


@pytest.mark.asyncio
async def test_re_enabling_preview_transitions_the_panel_to_media(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    assert settings_service.set_appearance_preview_enabled(False) is True
    appearance_service.reset_delivery_state()
    appearance_service.note_delivered(CHAT, MSG, ("text", ""))

    assert settings_service.set_appearance_preview_enabled(True) is True
    appearance_service.note_built("menu", ("media", version))

    async def _menu(event, extra):
        return ("LifeOS", "Choose a category:", [])

    _panel(monkeypatch, "menu", _menu)

    event = _Event()
    await panels.dispatch_click(event, "panel:menu", CHAT, MSG, OWNER)

    assert inline_calls == [(CHAT, "menu")]
    assert manager.sessions.get(CHAT, NEW_MSG) is not None
    assert appearance_service.delivered_state(CHAT, NEW_MSG) == ("media", version)


@pytest.mark.asyncio
async def test_a_failed_media_transition_keeps_the_existing_panel(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    first = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", first))
    assert await appearance_service.delivery_ready() is True
    ok, notice = await appearance_service.install_image(png_bytes(11, 9))
    assert ok is True, notice

    async def _failing(self_client, chat_id, query):
        inline_calls.append((chat_id, query))
        return (False, CHAT, 0, "")

    monkeypatch.setattr(inline_engine, "trigger", _failing)

    async def _menu(event, extra):
        return ("LifeOS", "Choose a category:", [])

    _panel(monkeypatch, "menu", _menu)

    event = _Event()
    await panels.dispatch_click(event, "panel:menu", CHAT, MSG, OWNER)

    # The replacement was attempted once, failed, and the existing panel lives on.
    assert inline_calls == [(CHAT, "menu")]
    assert client.deleted == []
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.get(CHAT, NEW_MSG) is None
    assert event.edits  # the owner still sees the (unchanged-identity) panel
    assert "Choose a category:" in event.edits[-1][0]


# ── the operational/customizable boundary ──────────────────────────────────

@pytest.mark.asyncio
async def test_dynamic_operational_content_is_not_treated_as_the_home_view(
    store, panel, monkeypatch
):
    await _install(store)
    assert appearance_service.set_header("title", "LifeOS HQ")[0] is True
    _action(monkeypatch, "_t_save_result", _save_result)

    event = _Event()
    paint = await panels._handle_action(event, "_t_save_result", CHAT, MSG, OWNER)
    assert paint is not None
    panel_id, _result, home_view = paint
    assert panel_id == "menu"
    assert home_view is False  # an action result is never a customizable page

    await panels.dispatch_click(event, "action:_t_save_result", CHAT, MSG, OWNER)
    assert "Saved S0042" in event.edits[-1][0]
    # The global header belongs to main-page home views, not to this result.
    assert "LifeOS HQ" not in event.edits[-1][0]


@pytest.mark.asyncio
async def test_home_navigation_still_works_after_an_operational_update(
    store, panel, inline_calls, monkeypatch
):
    manager, client = panel
    version = await _install(store)
    appearance_service.note_delivered(CHAT, MSG, ("media", version))
    manager.sessions.reset_nav(CHAT, MSG, "save", "")

    async def _save_panel(event, extra):
        return ("Save", "Reply to the message you want to save.", [])

    async def _menu(event, extra):
        return ("LifeOS", "Choose a category:", [])

    _panel(monkeypatch, "save", _save_panel)
    _panel(monkeypatch, "menu", _menu)

    event = _Event()
    await panels.dispatch_click(event, "panel:save", CHAT, MSG, OWNER)
    await panels.dispatch_click(event, "panel:_nav:home", CHAT, MSG, OWNER)

    assert inline_calls == []  # the media identity never changed
    assert manager.sessions.get(CHAT, MSG) is not None
    assert manager.sessions.current_nav(CHAT, MSG) == ("menu", "")
    assert "Choose a category:" in event.edits[-1][0]
