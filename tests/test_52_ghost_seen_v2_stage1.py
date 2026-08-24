from datetime import datetime, timezone
from types import SimpleNamespace

from backend.services.ghost_seen_v2 import (
    PAGE_SIZE, PrivateChat, filter_private_dialogs, matches_search, page_items,
    render_chat_row, render_browser, private_chat_from_dialog,
)

def user(**kwargs):
    return SimpleNamespace(**{"id": 1, "first_name": "Ali", "last_name": "Ahmadi", "username": "ali", "bot": False, "is_self": False, **kwargs})

def test_only_private_non_bot_users_are_included():
    dialogs = [
        SimpleNamespace(entity=user(id=2), is_user=True),
        SimpleNamespace(entity=user(id=3, bot=True), is_user=True),
        SimpleNamespace(entity=SimpleNamespace(id=4, title="Group"), is_user=False),
    ]
    filtered = filter_private_dialogs(dialogs, owner_id=1)
    assert [d.entity.id for d in filtered] == [2]
    assert private_chat_from_dialog(filtered[0], owner_id=1).display_name == "Ali Ahmadi"

def test_search_normalizes_names_and_username():
    chat = PrivateChat(2, "Ali", "Ahmadi", "Ali_A")
    assert matches_search(chat, " Ali   Ahmadi ")
    assert matches_search(chat, "AliAhmadi")
    assert matches_search(chat, "ahmadi")
    assert matches_search(chat, "@ali_a")
    assert matches_search(chat, "ALI_A")

def test_search_handles_missing_name_components():
    assert matches_search(PrivateChat(2, "Ali"), "Ali")
    assert matches_search(PrivateChat(3, "", "Ahmadi"), "Ahmadi")
    assert matches_search(PrivateChat(4, "", "", "user"), "@USER")
    assert matches_search(PrivateChat(5), "")

def test_rows_are_two_lines_and_preview_is_bounded():
    row = render_chat_row(PrivateChat(2, "A" * 100, "", preview="x" * 100, timestamp=datetime.now(timezone.utc), unread_count=3))
    assert len(row.splitlines()) == 2
    assert "…" in row
    assert "③" not in row
    assert row.endswith("3")

def test_pagination_is_capped_at_five():
    chats = [PrivateChat(i, str(i), timestamp=datetime.now(timezone.utc)) for i in range(PAGE_SIZE + 2)]
    first = page_items(chats)
    assert len(first.chats) == PAGE_SIZE
    assert (first.page, first.total_pages) == (1, 2)
    assert len(page_items(chats, 2).chats) == 2

def test_browser_has_empty_state_and_no_refresh_control():
    body, view = render_browser([], watcher_count=0)
    assert "No allowed chats yet" in body
    assert "Refresh" not in body
    assert view.page == view.total_pages == 1
