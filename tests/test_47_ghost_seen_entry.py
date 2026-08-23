"""
Execution — Ghost Seen Glass UI Entry & Management.

Focused regression tests for the missing UI integration:
  1. The main Glass UI menu exposes a visible Ghost Seen button.
  2. The button's callback (`panel:ghost_seen`) opens the EXISTING ghost panel
     through the standard panel dispatch (edit-in-place, no new message).
  3. Exactly one `ghost` panel registration and one router registration.
  4. All existing Ghost Seen actions/inputs remain reachable.
  5. Pre-existing menu entries remain intact.
  6. GHOST_ROOM_ID destination routing is untouched by the menu change.

The production change is a single button row in
`misc._build_menu_buttons()`; these tests pin that invariant so the
Ghost Seen panel can never become unreachable again.
"""
from __future__ import annotations

import inspect
import os

import pytest


# ── helpers ──


def _register_ghost_seen_once() -> None:
    """Register Ghost Seen panels/actions against a dummy client.

    Idempotent against test ordering: other suites may call ``configure()``
    (setting module globals) without registering panels, so the guard checks
    the live panel registry rather than module state.
    """
    from backend.helper.panel_registry import get_panel_def
    from backend.bot.handlers import ghost_seen

    if get_panel_def("ghost_seen") is not None:
        return

    class DummyClient:
        def on(self, *args, **kwargs):
            def deco(fn):
                return fn
            return deco

    ghost_seen.register(DummyClient(), 12345, "UTC")


async def _menu_buttons() -> tuple[str, list]:
    from backend.bot.handlers import misc

    title, body, buttons = await misc._menu_panel_handler(None, "")
    return title, buttons


# ── 1. Menu entry exists ──


class TestMenuEntry:
    @pytest.mark.asyncio
    async def test_menu_contains_ghost_seen_button(self):
        _title, buttons = await _menu_buttons()
        labels = [b.text for row in buttons for b in row]
        assert "👻 Ghost Seen" in labels

    @pytest.mark.asyncio
    async def test_entry_callback_is_panel_ghost(self):
        _title, buttons = await _menu_buttons()
        datas = [b.data for row in buttons for b in row]
        assert any(d == b"panel:ghost_seen" for d in datas)

    @pytest.mark.asyncio
    async def test_exactly_one_ghost_entry_in_menu(self):
        _title, buttons = await _menu_buttons()
        datas = [b.data for row in buttons for b in row]
        assert sum(1 for d in datas if d == b"panel:ghost_seen") == 1

    @pytest.mark.asyncio
    async def test_existing_menu_entries_preserved(self):
        _title, buttons = await _menu_buttons()
        datas = [b.data for row in buttons for b in row]
        for expected in (
            b"panel:save",
            b"panel:del",
            b"panel:profile",
            b"panel:db",
            b"panel:general",
            b"panel:settings",
            b"panel:ai",
        ):
            assert expected in datas, f"missing menu entry {expected!r}"


# ── 2. Registration integrity ──


class TestRegistrationIntegrity:
    def test_single_ghost_panel_registration_call_site(self):
        from pathlib import Path

        src = (
            Path("backend/bot/handlers/ghost_seen.py").read_text(encoding="utf-8")
        )
        assert src.count('register_panel("ghost_seen"') == 1
        assert src.count('register_panel("ghost_chat"') == 1

    def test_router_registers_ghost_seen_module_once(self):
        from pathlib import Path

        src = Path("backend/bot/router.py").read_text(encoding="utf-8")
        assert src.count("ghost_seen.register") == 1

    def test_registry_resolves_ghost_to_existing_handler(self):
        from backend.helper.panel_registry import get_panel_def
        from backend.bot.handlers import ghost_seen

        _register_ghost_seen_once()

        panel_def = get_panel_def("ghost_seen")
        assert panel_def is not None
        assert panel_def.render_function is ghost_seen._ghost_list_panel_handler
        assert panel_def.parent_panel == "menu"

    def test_all_ghost_actions_remain_registered(self):
        from backend.helper.panels import get_action

        _register_ghost_seen_once()

        for action_id in (
            "ghost_open",
            "ghost_toggle",
            "ghost_page",
            "ghost_clear",
            "ghost_back",
            "ghost_actions",
            "ghost_ctx",
            "ghost_remove",
        ):
            assert get_action(action_id) is not None, action_id

    def test_all_ghost_inputs_remain_registered(self):
        from backend.helper.panels import get_input

        _register_ghost_seen_once()

        for input_id in ("reply", "reply_no_quote", "ai_prompt"):
            cfg = get_input("ghost_chat", input_id)
            assert cfg is not None, input_id
            assert callable(cfg["handler"])


# ── 3. Pressing the entry opens the existing panel (edit-in-place) ──


class TestEntryNavigation:
    @pytest.mark.asyncio
    async def test_panel_ghost_dispatch_renders_ghost_list(self):
        from backend.helper.panels import _handle_panel
        from backend.helper.lifecycle import get_lifecycle

        _register_ghost_seen_once()

        rendered: dict = {}

        class FakeCbEvent:
            sender_id = 12345
            chat_id = 111
            message_id = 222
            original_update = None
            data = b"panel:ghost_seen"

            async def answer(self):
                pass

            async def edit(self, text, buttons=None):
                rendered["text"] = text
                rendered["buttons"] = buttons

        lifecycle = get_lifecycle()
        lifecycle.sessions.create(111, 222, panel_type="menu", owner_id=12345)

        await _handle_panel(FakeCbEvent(), "ghost_seen", 111, 222, 12345)

        assert "Ghost Seen" in rendered.get("text", "")

    @pytest.mark.asyncio
    async def test_navigation_uses_edit_not_new_message(self):
        from backend.helper import panels as panels_mod

        # The dispatch path must edit the callback message, never send one.
        src = inspect.getsource(panels_mod._handle_panel)
        assert ".send_message(" not in src

    @pytest.mark.asyncio
    async def test_empty_registry_renders_honest_empty_state(self):
        from backend.bot.handlers.ghost_seen import _ghost_list_panel_handler

        old = os.environ.get("GHOST_ROOM_ID")
        try:
            os.environ.pop("GHOST_ROOM_ID", None)
            result = await _ghost_list_panel_handler(None, "")
            assert result is not None
            title, body, _buttons = result
            assert title == "👻 Ghost Seen"
            assert "No private chats yet." in body
        finally:
            if old is not None:
                os.environ["GHOST_ROOM_ID"] = old


# ── 4. Destination routing untouched ──


class TestDestinationRoutingUntouched:
    def test_menu_module_has_no_destination_logic(self):
        from pathlib import Path

        misc_src = Path("backend/bot/handlers/misc.py").read_text(encoding="utf-8")
        assert "GHOST_ROOM_ID" not in misc_src
        assert "_resolve_ghost_destination" not in misc_src
        assert "execute_ghost_seen_ai" not in misc_src

    def test_ghost_service_still_owns_destination_resolution(self):
        from backend.bot.handlers.ghost_seen import (
            _resolve_ghost_destination,
        )

        old = os.environ.get("GHOST_ROOM_ID")
        try:
            os.environ["GHOST_ROOM_ID"] = "424242"
            assert _resolve_ghost_destination() == 424242
            os.environ["GHOST_ROOM_ID"] = "not-a-number"
            assert _resolve_ghost_destination() is None
            os.environ.pop("GHOST_ROOM_ID", None)
            assert _resolve_ghost_destination() is None
        finally:
            if old is not None:
                os.environ["GHOST_ROOM_ID"] = old
            else:
                os.environ.pop("GHOST_ROOM_ID", None)

    def test_menu_button_data_never_carries_a_chat_id(self):
        # The entry must be a bare panel navigation target — it must not
        # embed any chat id that could act as a destination selector.
        import re
        from pathlib import Path

        misc_src = Path("backend/bot/handlers/misc.py").read_text(encoding="utf-8")
        ghost_rows = [
            line for line in misc_src.splitlines() if "panel:ghost_seen" in line
        ]
        assert len(ghost_rows) == 1
        assert not re.search(r"panel:ghost:\d+", ghost_rows[0])
