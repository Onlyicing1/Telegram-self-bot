"""The finished Save panel: real result details plus its next actions.

Contract under test:

  * a successful Reply Mode save renders the item's own code, type and filename
    on the SAME panel, followed by the five required actions in order:
    Edit this item, Save another item, Show the saved items, Home, Close;
  * each action's callback reaches an EXISTING handler and targets real state —
    the item just saved (by its save code), the Save selection flow, the
    saved-items list, and the panel's own Home/Close navigation;
  * every label is the project's registry glyph + plain text: no emoji, no
    variation selector;
  * every button uses the documented style contract (all PRIMARY — the three
    Telegram styles encode confirm/cancel intent only);
  * the panel NEVER sends a progress/result message, on either edit path;
  * the Helper Bot's inline edit is the path that can replace the keyboard; the
    self-client fallback renders the same details as text only.

The fakes implement pinned Telethon 1.43.0's argument shapes. These tests prove
application behaviour; they do not prove Telegram accepts the edit.
"""
from __future__ import annotations

import asyncio
import unicodedata

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types

from backend.bot.handlers import retrieve as retrieve_handler
from backend.bot.handlers import save as save_handler
from backend.db import client as db_client
from backend.helper import inline_engine, input_state, panel_edit, symbols as sym
from backend.helper import panels as panels_module
from backend.helper.panel_edit import ExistingPanelEditor
from backend.helper.panel_render import render_edit
from backend.services import save_service

from tests.test_save_album_view_once_progress import (
    PANEL_CHAT,
    PANEL_MSG,
    FakeClient,
    FakeMessage,
    photo_message,
)
from tests.test_save_reply_panel_edit import (
    OWNER,
    REPLY_ID,
    TARGET_ID,
    RecordingHelper,
    _inline_id,
    _run,
    _wire,
)

EXPECTED_ACTIONS = [
    ("Edit this item", "panel:retrieve_item:id:{code}"),
    ("Save another item", "panel:save:type:d"),
    ("Show the saved items", "panel:retrieve_saved"),
    ("Home", "panel:_nav:home"),
    ("Close", "panel:_nav:close"),
]


# ── helpers ──


def _registry_glyphs() -> set[str]:
    glyphs: set[str] = set()
    for name in (
        "TASK_STATE_SYMBOLS",
        "SCHEDULE_SYMBOLS",
        "CONTENT_SYMBOLS",
        "OPERATION_SYMBOLS",
        "STRUCTURE_SYMBOLS",
    ):
        for spec in getattr(sym, name).values():
            glyphs.add(spec.glyph)
    return glyphs


def _runs_save(*, inline: bool = True, client=None, monkeypatch=None, helper=None):
    helper = helper or RecordingHelper()
    target, reply = (
        photo_message(TARGET_ID, text="hello", download_size=200_000),
        FakeMessage(REPLY_ID, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=TARGET_ID),
    )
    client = client or FakeClient([target, reply])
    _wire(client)
    if monkeypatch is not None:
        monkeypatch.setattr(save_handler, "get_client", lambda: helper)
    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG,
            inline_id=_inline_id() if inline else None,
        )
    )
    return helper, client


def _last_buttons(helper: RecordingHelper) -> list:
    return helper.calls[-1]["buttons"] or []


def _button_pairs(rows: list) -> list[tuple[str, str]]:
    return [
        (row[0].text, row[0].data.decode() if isinstance(row[0].data, bytes) else row[0].data)
        for row in rows
    ]


def _saved_code() -> str:
    return db_client._fallback["saved_items"][-1]["save_code"]


class _OnClient:
    """Enough of a Telethon client for a handler module to register itself."""

    def on(self, builder):
        def decorator(func):
            return func

        return decorator


@pytest.fixture(autouse=True)
def reset_state():
    # The real registration path: the panels/actions these buttons address only
    # exist once the handlers register themselves (what router.register_all does).
    from backend.bot.handlers import misc as misc_handler

    save_handler.register(_OnClient(), OWNER, "UTC")
    retrieve_handler.register(_OnClient(), OWNER)
    misc_handler.register(_OnClient(), OWNER)
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    input_state.clear_all()
    panel_edit.reset_panel_operations()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)
    yield
    input_state.clear_all()
    panel_edit.reset_panel_operations()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)


# ── the five actions ──


def test_a_successful_save_renders_the_five_actions_in_order(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)

    pairs = _button_pairs(_last_buttons(helper))
    code = _saved_code()

    assert [text for text, _data in pairs] == [
        sym.label(sym.OP_EDIT, "Edit this item"),
        sym.label(sym.OP_SAVE, "Save another item"),
        sym.label(sym.OP_SEARCH, "Show the saved items"),
        sym.label(sym.OP_HOME, "Home"),
        sym.label(sym.OP_CLOSE, "Close"),
    ]
    assert [data for _text, data in pairs] == [
        callback.format(code=code) for _label, callback in EXPECTED_ACTIONS
    ]
    assert len(_last_buttons(helper)) == 5, "one row per action, in order"
    assert client.send_message_calls == []


def test_the_success_panel_preserves_the_saved_items_real_details(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)
    code = _saved_code()
    row = db_client._fallback["saved_items"][-1]

    text = helper.calls[-1]["text"]

    assert "Saved Successfully" in text
    assert f"`{code}`" in text
    assert f"**Type:** {row['media_type']}" in text
    assert "**Filename:**" in text
    assert f"photo_{code}.jpg" in text
    # Details AND actions arrive together: one terminal edit, no text-only
    # result that the buttons then have to catch up with.
    assert len(helper.calls[-1]["buttons"]) == 5
    assert sum("Saved Successfully" in call["text"] for call in helper.calls) == 1
    assert all(
        "Saved Successfully" not in call["text"]
        for call in helper.calls[:-1]
    ), "no intermediate result without its actions"


def test_the_terminal_panel_is_the_same_message_and_the_only_surface(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)

    assert client.send_message_calls == []
    assert client.edit_calls == []
    inline_ids = {id(call["entity"]) for call in helper.calls}
    assert len(inline_ids) == 1, "every edit — progress and success — hits one panel"
    assert all(call["entity"] is helper.calls[0]["entity"] for call in helper.calls)
    assert "Saved Successfully" in helper.calls[-1]["text"]
    assert "Saved Successfully" not in helper.calls[0]["text"], (
        "the loading line comes first, the finished panel last"
    )


# ── unicode + no-emoji contract ──


def test_success_labels_use_registry_glyphs_and_carry_no_emoji(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    glyphs = _registry_glyphs()
    for label, _data in _button_pairs(_last_buttons(helper)):
        glyph, separator, text = label.partition(" ")
        assert separator == " ", "labels are '<glyph> <text>'"
        assert glyph in glyphs, f"{glyph!r} is not from the symbol registry"
        assert text.isascii(), "label text stays plain ASCII"
        assert not any(0x1F000 <= ord(ch) <= 0x1FAFF for ch in label), "no emoji"
        assert "\ufe0f" not in label, "no emoji variation selector"
        assert unicodedata.category(glyph) in {"Sm", "So", "Po", "Ps", "Pe", "Sk", "Mn"}, (
            "glyphs stay functional marks, not letters"
        )


def test_the_success_text_uses_the_established_typography(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    text = helper.calls[-1]["text"]
    assert "**Saved Successfully**" in text, "the shared markdown heading style"
    assert "`" in text, "identifiers stay code-spanned"
    assert not any(0x1F000 <= ord(ch) <= 0x1FAFF for ch in text), "no emoji anywhere"


# ── button color contract ──


def test_success_button_styles_follow_the_documented_contract(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    buttons = [row[0] for row in _last_buttons(helper)]
    assert len(buttons) == 5
    for button in buttons:
        style = button.style
        assert style is not None, "every button carries an explicit style"
        assert style.bg_primary is True
        assert style.bg_success is None, "green is reserved for confirmations"
        assert style.bg_danger is None, "red is reserved for cancellations"


def test_the_keyboard_is_a_real_inline_markup_of_callback_buttons(monkeypatch):
    """Pinned Telethon must build a ReplyInlineMarkup from these rows."""
    loop = asyncio.new_event_loop()
    try:
        client = TelegramClient(
            StringSession(), 12345, "0" * 32, auto_reconnect=False, loop=loop
        )
        _text, built = render_edit("", "x", save_handler.success_action_rows("S0007"))
        markup = client.build_reply_markup(built)
    finally:
        loop.close()

    assert isinstance(markup, types.ReplyInlineMarkup)
    assert len(markup.rows) == 5
    assert all(
        isinstance(b, types.KeyboardButtonCallback)
        for row in markup.rows
        for b in row.buttons
    )


# ── every action reaches real, existing behaviour ──


def test_edit_this_item_opens_the_item_that_was_just_saved(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)
    code = _saved_code()

    pairs = dict(_button_pairs(_last_buttons(helper)))
    data = pairs[sym.label(sym.OP_EDIT, "Edit this item")]
    assert data == f"panel:retrieve_item:id:{code}"

    handler = panels_module.get_panel("retrieve_item")
    assert handler is not None, "the item detail/edit panel is registered"
    title, body, buttons = _run(handler(None, f"id:{code}"))
    assert title == "Item Preview"
    assert f"`{code}`" in body, "the panel opened the item that was just saved"
    datas = [b.data.decode() if isinstance(b.data, bytes) else b.data
             for row in buttons for b in row]
    assert f"input:retrieve_item:rename:{code}" in datas, "the edit flow is reachable"
    assert f"input:retrieve_item:tags:{code}" in datas


def test_save_another_item_reenters_the_existing_save_flow(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    pairs = dict(_button_pairs(_last_buttons(helper)))
    data = pairs[sym.label(sym.OP_SAVE, "Save another item")]
    assert data == "panel:save:type:d"

    handler = panels_module.get_panel("save")
    assert handler is not None
    title, _body, buttons = _run(handler(None, "type:d"))
    assert title == "Deep Save"
    datas = [b.data.decode() if isinstance(b.data, bytes) else b.data
             for row in buttons for b in row]
    assert "action:save_reply" in datas, "Reply Mode is still the same entry point"
    assert panels_module.get_action("save_reply") is not None


def test_show_the_saved_items_opens_the_existing_listing(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    pairs = dict(_button_pairs(_last_buttons(helper)))
    data = pairs[sym.label(sym.OP_SEARCH, "Show the saved items")]
    assert data == "panel:retrieve_saved"

    handler = panels_module.get_panel("retrieve_saved")
    assert handler is not None
    _title, body, buttons = _run(handler(None, ""))
    assert _saved_code() in body or "items" in body
    datas = [b.data.decode() if isinstance(b.data, bytes) else b.data
             for row in buttons for b in row]
    assert f"panel:retrieve_item:id:{_saved_code()}" in datas


def test_home_and_close_use_the_existing_navigation(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    pairs = dict(_button_pairs(_last_buttons(helper)))
    assert pairs[sym.label(sym.OP_HOME, "Home")] == "panel:_nav:home"
    assert pairs[sym.label(sym.OP_CLOSE, "Close")] == "panel:_nav:close"
    assert {"home", "close"} <= panels_module._NAV_ACTIONS, (
        "the router handles both through _handle_navigation"
    )


def test_every_success_action_is_a_registered_surface(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    for _label, data in _button_pairs(_last_buttons(helper)):
        if data.startswith("panel:_nav:"):
            assert data.split(":")[-1] in panels_module._NAV_ACTIONS
            continue
        assert data.startswith("panel:"), data
        panel_id = data[len("panel:"):].split(":", 1)[0]
        assert panels_module.get_panel(panel_id) is not None, f"{panel_id} is unregistered"


# ── the buttons really dispatch (callback router, not just registration) ──


class _ClickEvent:
    """The Helper Bot's inline-message event, as the router uses it."""

    def __init__(self, data: str = ""):
        self.data = data.encode()
        self.edits: list[dict] = []

    async def edit(self, text, buttons=None, **kwargs):
        self.edits.append({"text": text, "buttons": buttons})
        return True

    async def answer(self, *args, **kwargs):
        return True


@pytest.fixture(autouse=True)
def quiet_timer(monkeypatch):
    # The panel timer is orthogonal to routing; recording its input keeps the
    # countdown out of the way without narrowing what is being dispatched.
    monkeypatch.setattr(panels_module, "_sync_timer", lambda *a, **k: None)


def _dispatch(data: str) -> _ClickEvent:
    event = _ClickEvent(data)
    remainder = data[len("panel:"):]
    if remainder.startswith("_nav:"):
        action = remainder[len("_nav:"):]
        _run(panels_module.dispatch_click(event, f"panel:_nav:{action}", PANEL_CHAT, PANEL_MSG, OWNER))
    else:
        _run(panels_module.dispatch_click(event, f"panel:{remainder}", PANEL_CHAT, PANEL_MSG, OWNER))
    return event


def _datas(buttons: list | None) -> list[str]:
    out = []
    for row in buttons or []:
        for button in row:
            data = getattr(button, "data", None)
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


def test_clicking_edit_this_item_opens_the_just_saved_items_panel(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)
    code = _saved_code()

    data = dict(_button_pairs(_last_buttons(helper)))[sym.label(sym.OP_EDIT, "Edit this item")]
    event = _dispatch(data)

    assert event.edits, "the click rendered something on the same panel"
    text = event.edits[-1]["text"]
    assert "Item Preview" in text
    assert f"`{code}`" in text, "the item resolved by its OWN save code"
    assert f"input:retrieve_item:rename:{code}" in _datas(event.edits[-1]["buttons"])
    assert client.send_message_calls == [], "no new message for a navigation click"


def test_clicking_save_another_item_reenters_reply_mode(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)

    data = dict(_button_pairs(_last_buttons(helper)))[sym.label(sym.OP_SAVE, "Save another item")]
    event = _dispatch(data)

    text = event.edits[-1]["text"]
    assert "Deep Save" in text
    datas = _datas(event.edits[-1]["buttons"])
    assert "action:save_reply" in datas, "the same Reply Mode entry point"
    assert "input:save:meta" in datas
    assert client.send_message_calls == []


def test_clicking_show_the_saved_items_lists_the_saved_items(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)
    code = _saved_code()

    data = dict(_button_pairs(_last_buttons(helper)))[sym.label(sym.OP_SEARCH, "Show the saved items")]
    event = _dispatch(data)

    text = event.edits[-1]["text"]
    assert "Saved Items" in text
    assert f"panel:retrieve_item:id:{code}" in _datas(event.edits[-1]["buttons"]), (
        "the item that was just saved is listed"
    )
    assert client.send_message_calls == []


def test_clicking_home_returns_to_the_existing_menu(monkeypatch):
    helper, client = _runs_save(monkeypatch=monkeypatch)

    data = dict(_button_pairs(_last_buttons(helper)))[sym.label(sym.OP_HOME, "Home")]
    event = _dispatch(data)

    text = event.edits[-1]["text"]
    assert "LifeOS" in text, "the Helper Bot's own mother panel"
    assert panels_module.get_lifecycle().sessions.current_nav(PANEL_CHAT, PANEL_MSG)[0] == "menu"
    assert client.send_message_calls == []


def test_clicking_close_uses_the_established_close(monkeypatch):
    helper, _client = _runs_save(monkeypatch=monkeypatch)
    closed: list[tuple] = []

    async def fake_close(event, chat_id, msg_id, owner_id):
        closed.append((chat_id, msg_id, owner_id))

    monkeypatch.setattr(panels_module, "close_panel", fake_close)

    data = dict(_button_pairs(_last_buttons(helper)))[sym.label(sym.OP_CLOSE, "Close")]
    _dispatch(data)

    assert closed == [(PANEL_CHAT, PANEL_MSG, OWNER)], (
        "Close goes through the lifecycle close of THIS panel"
    )


# ── failure paths and the fallback surface ──


def test_a_failed_save_renders_no_actions(monkeypatch):
    async def exploding_save(*args, **kwargs):
        raise RuntimeError("upload rejected")

    monkeypatch.setattr(save_service, "execute_save", exploding_save)
    helper, client = _runs_save(monkeypatch=monkeypatch)

    assert _last_buttons(helper) == []
    assert "upload rejected" in helper.calls[-1]["text"]
    assert client.send_message_calls == []


def test_a_save_without_a_code_renders_details_only(monkeypatch):
    async def codedless_save(*args, **kwargs):
        return save_service.SaveOutcome("**Saved Successfully**", saved=True, save_code="")

    monkeypatch.setattr(save_service, "execute_save", codedless_save)
    helper, _client = _runs_save(monkeypatch=monkeypatch)

    assert _last_buttons(helper) == [], "no item code means no item-bound action"
    assert "Saved Successfully" in helper.calls[-1]["text"]


def test_a_refused_terminal_inline_edit_still_lands_the_details(monkeypatch):
    class TerminalRefused(RecordingHelper):
        async def edit_message(self, entity, message=None, text=None, *, buttons=None, **kw):
            await super().edit_message(entity, message, text, buttons=buttons, **kw)
            if "Saved Successfully" in (text or ""):
                raise RuntimeError("inline edit refused")
            return True

    helper = TerminalRefused()
    helper, client = _runs_save(monkeypatch=monkeypatch, helper=helper)

    assert helper.calls[-1]["buttons"], "the ONE terminal edit carried the actions"
    assert "Saved Successfully" in client.edit_calls[-1]["text"], (
        "a refused terminal inline edit falls back to the same message's details"
    )
    assert client.edit_calls[-1]["buttons"] is None
    assert client.send_message_calls == []


def test_the_self_client_fallback_renders_details_without_attaching_a_keyboard():
    helper, client = _runs_save(inline=False, monkeypatch=None, helper=RecordingHelper())

    assert helper.calls == [], "no identifier means no inline edit"
    assert client.send_message_calls == []
    assert "Saved Successfully" in client.edit_calls[-1]["text"]
    assert all(call["buttons"] is None for call in client.edit_calls), (
        "a user-account edit must not attempt to attach a bot keyboard"
    )


def test_the_editor_replaces_the_keyboard_only_when_told_to(monkeypatch):
    helper = RecordingHelper()
    editor = ExistingPanelEditor(
        chat_id=PANEL_CHAT, msg_id=PANEL_MSG, inline_id=_inline_id(), helper=helper,
    )

    _run(editor("progress"))
    _run(editor("done", save_handler.success_action_rows("S0001")))
    _run(editor("progress"))

    assert helper.calls[0]["buttons"] is None, "progress keeps the keyboard it found"
    assert len(helper.calls[1]["buttons"]) == 5
    assert helper.calls[2]["buttons"] is None


def test_a_text_identical_render_with_new_buttons_is_not_skipped():
    helper = RecordingHelper()
    editor = ExistingPanelEditor(
        chat_id=PANEL_CHAT, msg_id=PANEL_MSG, inline_id=_inline_id(), helper=helper,
    )

    _run(editor("same"))
    _run(editor("same", save_handler.success_action_rows("S0001")))

    assert len(helper.calls) == 2, "the markup change is a real edit"
