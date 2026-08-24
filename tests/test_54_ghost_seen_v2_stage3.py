from backend.services.ghost_seen_v2 import (
    MessageViewerPage,
    ViewerMessage,
    clear_selection,
    get_selected_ids,
    render_message_viewer,
    toggle_selection,
)


def test_zero_one_and_multiple_selection_states():
    clear_selection(10)
    assert get_selected_ids(10) == ()
    assert toggle_selection(10, 4) == (4,)
    assert toggle_selection(10, 5) == (4, 5)
    assert get_selected_ids(10) == (4, 5)
    assert toggle_selection(10, 4) == (5,)


def test_identical_text_messages_use_real_ids():
    clear_selection(10)
    assert toggle_selection(10, 21) == (21,)
    assert toggle_selection(10, 22) == (21, 22)


def test_selection_isolated_by_source_chat_and_clearable():
    clear_selection(10)
    clear_selection(11)
    toggle_selection(10, 1)
    toggle_selection(11, 1)
    assert get_selected_ids(10) == (1,)
    assert get_selected_ids(11) == (1,)
    clear_selection(10)
    assert get_selected_ids(10) == ()
    assert get_selected_ids(11) == (1,)


def test_selection_survives_same_source_page_and_header_has_no_ai_or_refresh():
    clear_selection(44)
    toggle_selection(44, 8)
    viewer = MessageViewerPage(44, (ViewerMessage(8, 44, "hello"),), 2, 3, get_selected_ids(44))
    body = render_message_viewer("Ali", viewer)
    assert "1 selected" in body
    assert "AI" not in body
    assert "Reply" not in body
    assert "Refresh" not in body
