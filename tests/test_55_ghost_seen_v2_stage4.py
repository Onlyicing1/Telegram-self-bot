from backend.services.ghost_seen_v2 import (
    action_menu_state,
    action_placeholder,
    clear_selection,
    get_selected_ids,
    toggle_selection,
)


def test_action_menu_requires_current_selection_and_preserves_source_identity():
    clear_selection(101)
    clear_selection(202)
    assert action_menu_state(101) is None
    toggle_selection(101, 7)
    toggle_selection(101, 8)
    assert action_menu_state(101) == (7, 8)
    assert action_menu_state(202) is None
    assert get_selected_ids(101) == (7, 8)


def test_stale_or_wrong_action_state_fails_closed():
    clear_selection(303)
    toggle_selection(303, 4)
    assert action_placeholder("reply", 303, (4,)) == "Coming in the next stage."
    clear_selection(303)
    assert action_menu_state(303) is None
    assert action_placeholder("ai_reply", 303, (4,)) == "That selection is no longer available."
    assert action_placeholder("reply", 404, (4,)) == "That selection is no longer available."


def test_reply_and_ai_reply_are_inert_placeholders():
    clear_selection(505)
    toggle_selection(505, 1)
    assert action_placeholder("reply", 505, get_selected_ids(505)) == "Coming in the next stage."
    assert action_placeholder("ai_reply", 505, get_selected_ids(505)) == "Coming in the next stage."
    assert "input:ghost_chat:ai_prompt" not in action_placeholder("reply", 505, (1,))
    assert "Type your instruction for the selected messages." not in action_placeholder("ai_reply", 505, (1,))
