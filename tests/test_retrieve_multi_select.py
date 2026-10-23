"""Multi-select in the Saved Items (Stash) browser.

Contract under test:

  * the established browse view keeps every row opening its OWN item panel, and
    adds one explicit "Select Multiple" entry;
  * the selection view renders the exact save codes that page presented, each
    as a toggle of exactly that code — tapping never opens or re-resolves an
    item, and no search is ever re-run;
  * the selection is per-owner UI state that survives pagination, is bounded,
    expires, and is dropped by Cancel / by a bulk action;
  * a bulk action executes through the EXISTING single-item service
    operations, only for the codes that were ticked, and reports each item's
    real outcome (missing / foreign / vanished items are failures, never a
    silent success);
  * every generated callback stays inside Telegram's 64-byte limit;
  * the single-item preview and delete path is untouched.

Everything here is application behaviour against the in-memory DB fallback and
fake clients. No live Telegram call is made or proven.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.bot.handlers import retrieve as handler
from backend.db import client as db_client
from backend.helper import inline_engine, selection_state, symbols as sym
from backend.services import retrieve_service

OWNER = 777
OTHER_OWNER = 999
CHAT = -100123


# ── helpers ────────────────────────────────────────────────────────────────

def _seed(code: str, *, name: str | None = None, owner: int = OWNER) -> dict:
    row = {
        "id": len(db_client._fallback["saved_items"]) + 1,
        "save_code": code,
        "owner_id": owner,
        "display_name": name or f"Item {code}",
        "file_name": f"{code}.jpg",
        "media_type": "Photo",
        "mime_type": "image/jpeg",
        "file_size": 2048,
        "caption": "",
        "saved_chat_id": -1000,
        "saved_msg_id": 400 + len(db_client._fallback["saved_items"]),
    }
    db_client._fallback["saved_items"].append(row)
    return row


def _codes() -> list[str]:
    return [row.get("save_code") for row in db_client._fallback["saved_items"]]


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons or []:
        for button in row:
            data = getattr(button, "data", None)
            out.append(data.decode("utf-8") if isinstance(data, bytes) else str(data))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons or [] for button in row]


async def _panel(extra: str = ""):
    return await handler._retrieve_saved_panel_handler(None, extra)


async def _toggle(code: str):
    """Tick/untick through the registered ACTION (never a panel query)."""
    return await handler._retrieve_toggle_action(None, code, CHAT)


def _delete_double(monkeypatch, *, remove: bool = True) -> list[str]:
    """A stand-in for the single-item delete that records its calls.

    The bulk layer must go through THIS operation for every code; the double
    removes the row exactly like the production Supabase path does (the plain
    in-memory fallback leaves the row behind — see the verification test).
    """
    calls: list[str] = []

    async def fake(self_client, owner_id, code):
        calls.append(code)
        if remove:
            db_client._fallback["saved_items"] = [
                row for row in db_client._fallback["saved_items"]
                if row.get("save_code") != code
            ]
        return f"{sym.OP_SAVE} Deleted `{code}`"

    monkeypatch.setattr(retrieve_service, "do_delete", fake)
    return calls


@pytest.fixture(autouse=True)
def _clean_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    selection_state.clear_all()
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_self_client(None)
    yield
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    selection_state.clear_all()
    inline_engine.set_self_client(None)


# ── 1. the single-item browser entry point is preserved ────────────────────

@pytest.mark.asyncio
async def test_browse_rows_still_open_the_item_and_offer_multi_select():
    _seed("S0001", name="Schedule")

    title, _body, buttons = await _panel("")

    assert title == "Saved Items"
    datas = _datas(buttons)
    assert "panel:retrieve_item:id:S0001" in datas
    assert "panel:retrieve_saved:select" in datas
    assert not any(data.startswith("action:retrieve_toggle:") for data in datas)


@pytest.mark.asyncio
async def test_single_item_preview_and_delete_are_unchanged():
    _seed("S0001", name="Schedule")

    _title, _body, buttons = await handler._retrieve_item_panel_handler(None, "id:S0001")
    datas = _datas(buttons)

    assert "action:retrieve_item_exec:S0001" in datas
    assert "input:retrieve_item:rename:S0001" in datas
    assert "action:retrieve_item_delete:S0001" in datas


# ── 2. selection view: exact codes, explicit toggles ───────────────────────

@pytest.mark.asyncio
async def test_select_view_toggles_the_exact_codes_the_page_presented():
    _seed("S0001", name="Schedule")
    _seed("S0002", name="Invoice")

    _title, body, buttons = await _panel("select")

    datas = _datas(buttons)
    assert "action:retrieve_toggle:S0001" in datas
    assert "action:retrieve_toggle:S0002" in datas
    assert not any(data.startswith("panel:retrieve_item:") for data in datas), (
        "select mode must not open an item; tapping toggles the code"
    )
    labels = _texts(buttons)
    assert any(label.startswith(sym.SELECT_EMPTY) and "S0001" in label for label in labels)
    assert "0 selected" in body


@pytest.mark.asyncio
async def test_toggle_selects_and_deselects_the_same_item():
    _seed("S0001", name="Schedule")

    _title, body, buttons = await _toggle("S0001")
    assert selection_state.codes(OWNER) == ("S0001",)
    assert "1 selected" in body
    assert any(label.startswith(sym.SELECT_MARKED) for label in _texts(buttons))

    _title, body, buttons = await _toggle("S0001")
    assert selection_state.codes(OWNER) == ()
    assert "0 selected" in body
    assert not any(label.startswith(sym.SELECT_MARKED) for label in _texts(buttons))


@pytest.mark.asyncio
async def test_selection_keeps_exact_codes_and_their_presented_labels():
    _seed("S0001", name="University Schedule")
    _seed("S0002", name="Dentist")

    await _panel("select")
    await _toggle("S0001")

    assert selection_state.selected(OWNER) == (
        ("S0001", f"{sym.MEDIA_PHOTO} University Schedule"),
    )


# ── 3. pagination keeps the selection on the exact page it was ticked ──────

@pytest.mark.asyncio
async def test_selection_survives_pagination_and_stays_on_its_page():
    for index in range(1, 11):
        _seed(f"S{index:04d}")

    await _panel("select")
    await _toggle("S0001")
    _title, page2_body, page2_buttons = await _panel("select:page:2")

    assert "1 selected" in page2_body
    page2_datas = _datas(page2_buttons)
    assert "action:retrieve_toggle:S0009" in page2_datas
    assert "action:retrieve_toggle:S0001" not in page2_datas

    _title, toggled_body, _buttons = await _toggle("S0009")
    assert selection_state.codes(OWNER) == ("S0001", "S0009")
    assert "page 2/2" in toggled_body, "a toggle returns to the page being viewed"

    _title, _page1_body, page1_buttons = await _panel("select:page:1")
    labels = _texts(page1_buttons)
    assert any(label.startswith(sym.SELECT_MARKED) and "S0001" in label for label in labels)
    assert any(label.startswith(sym.SELECT_EMPTY) and "S0002" in label for label in labels)


# ── 4. starting / canceling a session is explicit and consistent ───────────

@pytest.mark.asyncio
async def test_starting_a_new_selection_discards_the_previous_one():
    _seed("S0001")

    await _panel("select")
    await _toggle("S0001")
    _title, body, _buttons = await _panel("select")

    assert selection_state.codes(OWNER) == ()
    assert "0 selected" in body


@pytest.mark.asyncio
async def test_cancel_clears_the_selection_and_returns_to_the_browse_view():
    _seed("S0001")

    await _panel("select")
    await _toggle("S0001")
    _title, _body, buttons = await _panel("cancel")

    assert selection_state.codes(OWNER) == ()
    assert "panel:retrieve_item:id:S0001" in _datas(buttons)


@pytest.mark.asyncio
async def test_back_from_a_review_keeps_the_selection():
    _seed("S0001", name="Schedule")

    await _panel("select")
    await _toggle("S0001")
    _title, review_body, _buttons = await _panel("bulk:delete")
    assert "`S0001`" in review_body

    _title, body, _buttons = await _panel("back")
    assert selection_state.codes(OWNER) == ("S0001",)
    assert "1 selected" in body


# ── 5. stale / foreign / missing items never enter or survive a selection ──

@pytest.mark.asyncio
async def test_toggling_a_missing_item_selects_nothing():
    _seed("S0001")

    await _panel("select")
    _title, body, _buttons = await _toggle("S9999")

    assert selection_state.codes(OWNER) == ()
    assert "no longer in your saved items" in body


@pytest.mark.asyncio
async def test_toggling_another_owners_item_selects_nothing():
    _seed("S0001", owner=OTHER_OWNER)

    await _panel("select")
    _title, body, _buttons = await _toggle("S0001")

    assert selection_state.codes(OWNER) == ()
    # A foreign row is reported exactly like a missing one — no data leak.
    assert "no longer in your saved items" in body


@pytest.mark.asyncio
async def test_malformed_toggles_and_pages_are_survivable():
    _seed("S0001")

    for code in ("", "   ", "S"):
        title, _body, buttons = await _toggle(code)
        assert title == "Saved Items"
        assert isinstance(buttons, list)

    for extra in ("select:page:abc", "page:0", "nonsense"):
        title, _body, buttons = await _panel(extra)
        assert title == "Saved Items"
        assert isinstance(buttons, list)

    assert selection_state.codes(OWNER) == ()


# ── 6. bounded selection state ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_selection_is_bounded_and_the_refusal_is_reported():
    for index in range(1, 42):
        _seed(f"S{index:04d}")

    await _panel("select")
    for index in range(1, selection_state.MAX_SELECTION + 1):
        await _toggle(f"S{index:04d}")

    assert selection_state.count(OWNER) == selection_state.MAX_SELECTION

    overflow = f"S{selection_state.MAX_SELECTION + 1:04d}"
    _title, body, _buttons = await _toggle(overflow)

    assert overflow not in selection_state.codes(OWNER)
    assert "at most" in body
    assert f"{selection_state.MAX_SELECTION} selected" in body


def test_selection_store_ignores_malformed_input():
    assert selection_state.toggle(OWNER, "") == (False, "no saved item was named")
    assert selection_state.toggle(OWNER, "   ")[0] is False
    assert selection_state.toggle("not-an-owner", "S0001")[0] is False
    assert selection_state.selected(OWNER) == ()

    selection_state.toggle(OWNER, "S0001", "Schedule")
    assert selection_state.codes(OWNER) == ("S0001",)
    assert selection_state.clear(OWNER) == ("S0001",)
    assert selection_state.codes(OWNER) == ()


def test_selection_store_expires_after_its_ttl(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(selection_state, "_now", lambda: now)

    selection_state.toggle(OWNER, "S0001", "Schedule")
    assert selection_state.codes(OWNER) == ("S0001",)

    now += selection_state.SELECTION_TTL_SECONDS + 1

    assert selection_state.codes(OWNER) == ()
    assert selection_state.current_page(OWNER) == 1


# ── 7. bulk delete: confirm, exact identity, honest reporting ──────────────

@pytest.mark.asyncio
async def test_bulk_delete_requires_a_confirmation_and_touches_nothing():
    _seed("S0001")
    _seed("S0002")

    await _panel("select")
    await _toggle("S0001")
    await _toggle("S0002")
    title, body, buttons = await _panel("bulk:delete")

    assert title == "Delete Selected"
    assert "`S0001`" in body and "`S0002`" in body
    assert "action:retrieve_bulk_delete:confirm" in _datas(buttons)

    _title, result, _buttons = await handler._retrieve_bulk_delete_action(None, "", CHAT)

    assert "Confirmation is required" in result
    assert sorted(_codes()) == ["S0001", "S0002"]


@pytest.mark.asyncio
async def test_bulk_delete_removes_exactly_the_selected_items(monkeypatch):
    for code in ("S0001", "S0002", "S0003"):
        _seed(code)
    calls = _delete_double(monkeypatch)

    await _panel("select")
    await _toggle("S0001")
    await _toggle("S0003")

    _title, body, buttons = await handler._retrieve_bulk_delete_action(None, "confirm", CHAT)

    assert calls == ["S0001", "S0003"], "only the ticked codes reach the single-item delete"
    assert _codes() == ["S0002"], "the unselected item must survive"
    assert "`S0001`" in body and "`S0003`" in body
    assert "Deleted 2 items" in body
    assert body.startswith(sym.OP_SAVE)
    assert selection_state.codes(OWNER) == ()
    assert "panel:retrieve_saved" in _datas(buttons)


@pytest.mark.asyncio
async def test_bulk_delete_reports_a_vanished_item_instead_of_full_success(monkeypatch):
    _seed("S0001")
    stale = _seed("S0002")
    calls = _delete_double(monkeypatch)

    await _panel("select")
    await _toggle("S0001")
    await _toggle("S0002")
    db_client._fallback["saved_items"].remove(stale)

    _title, body, _buttons = await handler._retrieve_bulk_delete_action(None, "confirm", CHAT)

    assert calls == ["S0001"], "a vanished item is never sent to the delete operation"
    assert _codes() == []
    assert "Deleted 1 of 2 items" in body
    assert "`S0002`" in body
    assert "not found" in body


@pytest.mark.asyncio
async def test_bulk_delete_verifies_the_row_is_really_gone(monkeypatch):
    """A delete that leaves the saved row behind is reported as a failure."""
    _seed("S0001")
    calls = _delete_double(monkeypatch, remove=False)

    await _panel("select")
    await _toggle("S0001")

    _title, body, _buttons = await handler._retrieve_bulk_delete_action(None, "confirm", CHAT)

    assert calls == ["S0001"]
    assert "Deleted 0 of 1" in body
    assert "still present after deletion" in body


@pytest.mark.asyncio
async def test_bulk_delete_never_deletes_a_foreign_or_foreign_owner_item(monkeypatch):
    _seed("S0001", owner=OTHER_OWNER)
    calls = _delete_double(monkeypatch)
    selection_state.toggle(OWNER, "S0001", "Not mine")

    _title, body, _buttons = await handler._retrieve_bulk_delete_action(None, "confirm", CHAT)

    assert calls == [], "a foreign row never reaches the delete operation"
    assert "Deleted 0 of 1" in body
    assert _codes() == ["S0001"], "the other owner's row is untouched"
    assert selection_state.codes(OWNER) == ()


@pytest.mark.asyncio
async def test_a_single_item_delete_still_works_after_a_bulk_session(monkeypatch):
    _seed("S0001")
    _seed("S0002")
    calls = _delete_double(monkeypatch)

    await _panel("select")
    await _toggle("S0001")
    await _panel("cancel")
    _title, _body, _buttons = await handler._retrieve_item_delete_action(None, "S0001", CHAT)

    assert calls == ["S0001"]
    assert _codes() == ["S0002"]


# ── 8. bulk send: reuses the single-item retrieval, reports failures ───────

@pytest.mark.asyncio
async def test_bulk_send_calls_the_single_item_retrieval_once_per_selected_code(monkeypatch):
    for code in ("S0001", "S0002", "S0003"):
        _seed(code)

    await _panel("select")
    await _toggle("S0002")
    await _toggle("S0003")

    spy = AsyncMock(return_value=f"{sym.OP_SAVE} Retrieved `S0002` to this chat.")
    monkeypatch.setattr(retrieve_service, "do_retrieve", spy)

    _title, body, _buttons = await handler._retrieve_bulk_send_action(None, "confirm", CHAT)

    assert [call.args[2] for call in spy.await_args_list] == ["S0002", "S0003"]
    assert all(call.args[1] == OWNER and call.args[3] == CHAT for call in spy.await_args_list)
    assert "Sent 2 items" in body
    assert selection_state.codes(OWNER) == ()


@pytest.mark.asyncio
async def test_bulk_send_reports_service_failures_honestly(monkeypatch):
    _seed("S0001")
    _seed("S0002")

    await _panel("select")
    await _toggle("S0001")
    await _toggle("S0002")

    monkeypatch.setattr(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value=f"{sym.STATE_FAILED} Forward failed: no peer"),
    )

    _title, body, _buttons = await handler._retrieve_bulk_send_action(None, "confirm", CHAT)

    assert "Sent 0 of 2 items" in body
    assert "Forward failed" in body
    assert body.startswith(sym.STATE_FAILED)
    assert selection_state.codes(OWNER) == ()


@pytest.mark.asyncio
async def test_bulk_send_requires_confirmation_and_a_target_chat():
    _seed("S0001")
    await _panel("select")
    await _toggle("S0001")

    _title, no_chat, _buttons = await handler._retrieve_bulk_send_action(None, "confirm", 0)
    assert "Cannot determine target chat" in no_chat

    _title, unconfirmed, _buttons = await handler._retrieve_bulk_send_action(None, "", CHAT)
    assert "Confirmation is required" in unconfirmed


# ── 9. empty selections never claim to have done anything ─────────────────

@pytest.mark.asyncio
async def test_empty_selection_bulk_actions_do_nothing():
    _seed("S0001")

    _title, review_body, _buttons = await _panel("bulk:send")
    assert "No items are selected" in review_body

    _title, delete_body, _buttons = await handler._retrieve_bulk_delete_action(None, "confirm", CHAT)
    assert "No items are selected" in delete_body

    _title, send_body, _buttons = await handler._retrieve_bulk_send_action(None, "confirm", CHAT)
    assert "No items are selected" in send_body

    assert _codes() == ["S0001"]


# ── 10. service-level bulk contract ────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_service_dedupes_and_preserves_the_requested_order(monkeypatch):
    _seed("S0001")
    _seed("S0002")
    calls = _delete_double(monkeypatch)

    result = await retrieve_service.do_bulk_delete(
        MagicMock(), OWNER, ["s0001", "S0001", "  ", "S0002"],
    )

    assert calls == ["S0001", "S0002"]
    assert result.requested == 2
    assert [outcome.code for outcome in result.done] == ["S0001", "S0002"]


@pytest.mark.asyncio
async def test_bulk_service_bounds_the_request_and_reports_the_overflow(monkeypatch):
    attempted: list[str] = []

    async def precheck(owner_id, code):
        attempted.append(code)
        return None, retrieve_service._BULK_ALREADY_GONE

    monkeypatch.setattr(retrieve_service, "_bulk_precheck", precheck)

    result = await retrieve_service.do_bulk_delete(
        MagicMock(), OWNER, [f"S{index:04d}" for index in range(1, 43)],
    )

    assert len(attempted) == retrieve_service.MAX_BULK_ITEMS
    assert result.failed_count == 42
    assert "at most" in result.failed[-1].message


def test_bulk_formatting_never_turns_a_partial_result_into_success():
    result = retrieve_service.BulkResult(
        "delete",
        (retrieve_service.BulkItemOutcome("S0001", True, "ok"),),
        (retrieve_service.BulkItemOutcome("S0002", False, "not found"),),
    )

    text = retrieve_service.format_bulk_result(result)

    assert "Deleted 1 of 2 items" in text
    assert "`S0002`" in text
    assert "not found" in text
    assert not text.startswith(sym.OP_SAVE)


# ── 11. callback-data budget and routing conventions ──────────────────────

@pytest.mark.asyncio
async def test_a_tick_is_an_action_with_a_bounded_callback():
    """A tick must not be a panel query: panel extras land on the nav stack,
    which would make Back re-run the toggle and untick an item."""
    _seed("S0001")
    handler.register(None, OWNER)

    from backend.helper.panels import get_action
    assert get_action("retrieve_toggle") is handler._retrieve_toggle_action

    await _panel("select")
    _title, _body, buttons = await _toggle("S0001")

    datas = _datas(buttons)
    assert "action:retrieve_toggle:S0001" in datas
    assert not any(data.startswith("panel:retrieve_saved:toggle:") for data in datas)
    for data in datas:
        assert len(data.encode("utf-8")) <= 64, data


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["", "select", "select:page:2", "bulk:send", "bulk:delete"])
async def test_every_multi_select_callback_fits_telegram_and_uses_a_known_prefix(extra):
    for index in range(1, 11):
        _seed(f"S{index:04d}")

    await _panel("select")
    if extra.startswith("bulk"):
        await _toggle("S0001")
        await _panel("select:page:2")

    _title, _body, buttons = await _panel(extra)

    for data in _datas(buttons):
        assert len(data.encode("utf-8")) <= 64, data
        assert data.startswith(("panel:", "action:")), data
