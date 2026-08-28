"""Ghost Seen v2 — bounded Manage UI regression tests.

The Manage panel must NEVER render one button per private chat. It renders
at most ``MANAGE_PAGE_SIZE`` (8) chat rows per page, with Previous/Next
navigation and a search input.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.bot.handlers import ghost_seen_v2 as handler_module
from backend.services import ghost_seen_v2 as service_module
from backend.services.ghost_seen_v2 import (
    MANAGE_PAGE_SIZE, PrivateChat, allow_chat, manage_page_items,
    reset_allowed_chats,
)


def _chats(count: int, offset: int = 0) -> list[PrivateChat]:
    return [PrivateChat(chat_id=1000 + i + offset, first_name=f"User{i + offset}") for i in range(count)]


def manage_page(chats, page: int = 1, query: str = ""):
    return manage_page_items(chats, page, query)


def reset_allowed():
    reset_allowed_chats()


def _button_texts(buttons: list) -> list[str]:
    return [getattr(btn, "text", "") for row in buttons for btn in row]


def _event(chat_id: int, message_id: int):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id)


def _run(coro):
    return asyncio.run(coro)


def test_1_manage_page_size_constant():
    assert MANAGE_PAGE_SIZE == 8


def test_2_manage_page_caps_rows():
    view = manage_page(_chats(500))
    assert len(view.chats) == 8
    assert view.total_pages == 63
    assert view.page == 1


def test_3_first_page_contains_first_eight_names():
    view = manage_page(_chats(50))
    # Sorted lexicographically by display name.
    assert [chat.display_name for chat in view.chats] == ["User0", "User1", "User10", "User11", "User12", "User13", "User14", "User15"]


def test_4_middle_and_last_page():
    view = manage_page(_chats(30), page=2)
    assert len(view.chats) == 8
    assert view.page == 2
    assert view.total_pages == 4
    last = manage_page(_chats(30), page=4)
    assert len(last.chats) == 6
    assert last.page == 4


def test_5_one_page_case_has_no_overflow():
    view = manage_page(_chats(5))
    assert view.total_pages == 1
    assert len(view.chats) == 5


def test_6_empty_case():
    view = manage_page([])
    assert view.chats == ()
    assert view.total_pages == 1


def test_7_manage_sorted_by_display_name():
    chats = [PrivateChat(1, first_name="Zed"), PrivateChat(2, first_name="Ali"), PrivateChat(3, first_name="Sara")]
    view = manage_page(chats)
    assert [c.display_name for c in view.chats] == ["Ali", "Sara", "Zed"]


def test_8_manage_search_narrows_list():
    chats = _chats(20) + [PrivateChat(999, first_name="AliReza")]
    view = manage_page(chats, query="Ali")
    assert all(c.display_name == "AliReza" for c in view.chats)
    assert view.query == "Ali"


def test_9_manage_search_no_match_empty():
    view = manage_page(_chats(10), query="nobody")
    assert view.chats == ()


def test_10_render_manage_bounded_with_500_chats():
    """The 500-button panel bug regression: never one button per chat."""
    title, body, buttons = handler_module._render_manage(_chats(500))
    texts = _button_texts(buttons)
    chat_rows = [t for t in texts if t[:2].rstrip(".").isdigit()]
    assert len(chat_rows) <= MANAGE_PAGE_SIZE
    assert len(buttons) <= MANAGE_PAGE_SIZE + 4  # chat rows + nav/search/back
    assert "1/63" in " ".join(texts)


def test_11_render_manage_many_pages_still_bounded():
    for total in (100, 200, 500):
        title, body, buttons = handler_module._render_manage(_chats(total))
        assert len(buttons) <= MANAGE_PAGE_SIZE + 4


def test_12_render_manage_shows_on_off_state():
    reset_allowed()
    allow_chat(_chats(3)[1].chat_id)
    title, body, buttons = handler_module._render_manage(_chats(3))
    texts = _button_texts(buttons)
    assert sum(1 for t in texts if "ON" in t) == 1
    assert sum(1 for t in texts if "OFF" in t) == 2


def test_13_render_manage_empty():
    title, body, buttons = handler_module._render_manage([])
    assert "No private chats found" in body
    texts = _button_texts(buttons)
    assert not any(t[:2].rstrip(".").isdigit() for t in texts)
    assert any("Back" in t for t in texts)


def test_14_render_manage_search_note():
    title, body, buttons = handler_module._render_manage(_chats(10), query="User")
    assert "Search: User" in body


def test_15_manage_buttons_nav_only_when_needed():
    middle = handler_module._manage_buttons(manage_page(_chats(20), page=2))
    texts = _button_texts(middle)
    assert any("Previous" in t for t in texts)
    assert any("Next" in t for t in texts)
    assert any("2/3" in t for t in texts)
    first = handler_module._manage_buttons(manage_page(_chats(20)))
    texts_first = _button_texts(first)
    assert not any("Previous" in t for t in texts_first)
    assert any("Next" in t for t in texts_first)
    single = handler_module._manage_buttons(manage_page(_chats(3)))
    texts_single = _button_texts(single)
    assert not any("Previous" in t for t in texts_single)
    assert not any("Next" in t for t in texts_single)


def test_16_manage_buttons_always_have_back_and_search():
    buttons = handler_module._manage_buttons(manage_page(_chats(5)))
    texts = _button_texts(buttons)
    assert any("Back" in t for t in texts)
    assert any("Search" in t for t in texts)


def test_17_manage_page_action_navigates():
    with patch.object(service_module, "load_private_chats", new=AsyncMock(return_value=_chats(30))):
        with patch.object(handler_module, "_session_extra", return_value="p=1&q="):
            with patch.object(handler_module, "_set_extra") as set_extra:
                service_module.invalidate_manage_directory()
                title, body, buttons = _run(handler_module._manage_page_action(_event(3, 1), "2", 3))
                texts = _button_texts(buttons)
                page_two_names = [c.display_name for c in manage_page(_chats(30), page=2).chats]
                assert any(name in t for name in page_two_names for t in texts)
                assert any("2/4" in t for t in texts)
                set_extra.assert_called_once()


def test_18_toggle_preserves_page_and_query():
    with patch.object(service_module, "load_private_chats", new=AsyncMock(return_value=_chats(30))):
        with patch.object(handler_module, "_session_extra", return_value="p=2&q="):
            reset_allowed()
            service_module.invalidate_manage_directory()
            target = manage_page(_chats(30), page=2).chats[0].chat_id
            async def allow_and_persist(chat_id):
                allow_chat(chat_id)
                return True
            with patch.object(service_module, "allow_chat_and_persist", new=allow_and_persist):
                title, body, buttons = _run(handler_module._toggle_permission_action(_event(777, 5), str(target), 777))
            texts = _button_texts(buttons)
            assert any("ON" in t for t in texts)
            assert any("2/4" in t for t in texts)


def test_19_toggle_off_removes_chat_from_page():
    with patch.object(service_module, "load_private_chats", new=AsyncMock(return_value=_chats(30))):
        with patch.object(handler_module, "_session_extra", return_value="p=1&q="):
            reset_allowed()
            service_module.invalidate_manage_directory()
            target = _chats(30)[0].chat_id  # User0 → page 1
            allow_chat(target)
            async def disallow_and_persist(chat_id):
                service_module._allowed_chats.discard(chat_id)
                return True
            with patch.object(service_module, "disallow_chat_and_persist", new=disallow_and_persist):
                title, body, buttons = _run(handler_module._toggle_permission_action(_event(777, 5), str(target), 777))
            texts = _button_texts(buttons)
            assert not any("User0" in t and "ON" in t for t in texts)
            assert any("User0" in t and "OFF" in t for t in texts)


def test_20_no_refresh_button_in_manage():
    buttons = handler_module._manage_buttons(manage_page(_chats(20)))
    assert all("Refresh" not in getattr(b, "text", "") for row in buttons for b in row)


def test_21_legacy_absence_unchanged():
    for mod in (service_module, handler_module):
        src = inspect.getsource(mod)
        assert "input:ghost_chat:ai_prompt" not in src
        assert "GHOST_ROOM_ID" not in src
        assert "ai_prompt" not in src
        assert "Type your instruction for the selected messages." not in src