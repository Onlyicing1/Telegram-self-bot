"""History & Intelligence — P1 manual and link-based Glass UI tests.

Pins the actual P1 boundaries:

* one registered panel under the menu, reusing the existing registries;
* bounded dialog enumeration and honest empty/failure states;
* a manual run through ``history_ai_service.summarize_history`` with
  ``current_message_id=None`` and the selected count/focus;
* a link run that resolves through the existing link resolver and passes
  ``linked_id + 1`` as the EXCLUSIVE upper bound, so the linked message is
  included and nothing newer is;
* provider/retrieval/timeout failures never become a fabricated summary;
* result delivery edits the ORIGINAL History panel through the Helper Bot's
  existing inline edit (``messages.editInlineBotMessage``), never the owner's
  self-bot client, never a replacement panel and never Saved Messages;
* a background operation is bounded and cannot overwrite a newer panel state.

No live Telegram, provider or database is used.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from telethon.tl import types

from backend.bot.handlers import history
from backend.helper import lifecycle as lifecycle_module
from backend.services import appearance_service
from backend.helper.panel_edit import supersede_panel_operation
from backend.helper.panels import get_action, get_input, get_panel
from backend.helper.panel_registry import get_panel_parent, get_panel_title
from backend.services import save_service

OWNER = 7770001
CHAT = -100555000
PANEL_CHAT = 7770001
PANEL_MSG = 99
USER_MSG = 100

#: The inline-bot handle Telegram hands the Helper Bot when the panel's button
#: is clicked. It is the ONLY supported way to edit an inline result message.
INLINE_ID = types.InputBotInlineMessageID64(
    dc_id=2, owner_id=OWNER, id=PANEL_MSG, access_hash=123456789,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _HelperClient:
    """The Helper Bot, as the History delivery path actually uses it."""

    def __init__(self) -> None:
        self.edits: list[tuple] = []
        self.error: Exception | None = None

    async def edit_message(self, entity, text=None, buttons=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.edits.append((entity, text, buttons))

    @property
    def last_text(self) -> str:
        return self.edits[-1][1] if self.edits else ""


_helper = _HelperClient()


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(history, "_STATE", history._State())
    monkeypatch.setattr(history, "get_client", lambda: _helper)
    monkeypatch.setattr(history.utils, "get_peer_id", lambda entity: int(entity.id))
    _helper.edits.clear()
    _helper.error = None
    appearance_service.reset_delivery_state()
    yield
    _helper.edits.clear()
    _helper.error = None
    appearance_service.reset_delivery_state()


class _Client:
    def __init__(self, dialogs=None, *, fail_dialogs=False):
        self.dialogs = list(dialogs or [])
        self.fail_dialogs = fail_dialogs
        self.dialog_limits: list[int] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deleted: list[tuple[int, list[int]]] = []

    async def get_dialogs(self, limit=None):
        self.dialog_limits.append(limit)
        if self.fail_dialogs:
            raise ConnectionError("mtproto down")
        return self.dialogs[:limit]

    async def edit_message(self, chat_id, msg_id, text=None, **kwargs):
        self.edits.append((chat_id, msg_id, text))

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))

    @property
    def last_text(self) -> str:
        return self.edits[-1][2] if self.edits else ""


def _dialog(entity, *, name="", user=False, group=False, channel=False):
    return SimpleNamespace(
        entity=entity,
        name=name,
        is_user=user,
        is_group=group,
        is_channel=channel,
    )


def _user(uid=1234, name="Alice"):
    return _dialog(SimpleNamespace(id=uid, first_name=name), name=name, user=True)


def _group(gid=555, name="Team"):
    return _dialog(SimpleNamespace(id=gid, title=name, megagroup=False), name=name, group=True)


def _supergroup(cid=666, name="News"):
    return _dialog(
        SimpleNamespace(id=cid, title=name, megagroup=True), name=name, channel=True,
    )


def _event():
    return SimpleNamespace(
        sender_id=OWNER,
        chat_id=PANEL_CHAT,
        message_id=PANEL_MSG,
        message=SimpleNamespace(id=PANEL_MSG),
        query=SimpleNamespace(msg_id=INLINE_ID),
        raw_text="",
    )


def _buttons(result) -> list[str]:
    _title, _body, rows = result
    out = []
    for row in rows:
        for button in row:
            data = getattr(button, "data", b"")
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


def _labels(result) -> list[str]:
    _title, _body, rows = result
    return [getattr(button, "text", "") for row in rows for button in row]


def _chat(peer_id: int, name: str) -> history._Chat:
    return history._Chat(peer_id=peer_id, name=name, kind="private")


# ── registration and navigation ─────────────────────────────────────────────

def test_the_history_panel_and_controls_are_registered_once():
    history.register(None, OWNER)
    assert get_panel("history") is history._panel_handler
    assert get_panel_parent("history") == "menu"
    assert get_panel_title("history") == "History & Intelligence"
    assert get_action("history_run") is history._run_action
    assert get_action("history_count") is history._count_action
    assert get_input("history", "focus")["handler"] is history._focus_input_handler
    assert get_input("history", "link")["handler"] is history._link_input_handler


def test_the_main_menu_has_one_history_entry():
    from backend.bot.handlers import misc

    result = ("LifeOS", "", misc._build_menu_buttons())
    assert "panel:history" in _buttons(result)
    assert "History & Intelligence" in _labels(result)


def test_the_initial_panel_renders_the_unselected_state():
    title, body, buttons = history._panel_view()
    assert title == "History & Intelligence"
    assert "Chat: not selected" in body
    assert "Messages: 100" in body
    assert "panel:history:chats:0" in [b for b in _buttons((title, body, buttons))]
    assert "action:history_run" in _buttons((title, body, buttons))


# ── dialogs ────────────────────────────────────────────────────────────────

def test_dialog_enumeration_is_bounded_and_selectable(monkeypatch):
    client = _Client([_user(), _group(), _supergroup()])
    monkeypatch.setattr(history, "get_self_client", lambda: client)

    title, body, buttons = _run(history._panel_handler(None, "chats:0"))
    assert client.dialog_limits == [history.DIALOG_SCAN_LIMIT]
    labels = " ".join(_labels((title, body, buttons)))
    assert "Alice" in labels and "Team" in labels and "News" in labels
    datas = _buttons((title, body, buttons))
    assert any(data.startswith("panel:history:pick:") for data in datas)

    peer = history._Chat(peer_id=next(iter(client.dialogs)).entity.id, name="Alice", kind="private")
    selected = _run(history._pick_chat(str(peer.peer_id)))
    assert history._STATE.chat_id == peer.peer_id
    assert history._STATE.chat_name == "Alice"
    assert "Selected: Alice" in selected[1]


def test_an_empty_dialog_list_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(history, "get_self_client", lambda: _Client([]))
    _title, body, buttons = _run(history._panel_handler(None, "chats:0"))
    assert "No accessible chats" in body
    assert not any(data.startswith("panel:history:pick:") for data in _buttons((_title, body, buttons)))


def test_dialog_enumeration_failure_never_fabricates_entries(monkeypatch):
    monkeypatch.setattr(history, "get_self_client", lambda: _Client(fail_dialogs=True))
    _title, body, buttons = _run(history._panel_handler(None, "chats:0"))
    assert "could not be read" in body
    assert not any(data.startswith("panel:history:pick:") for data in _buttons((_title, body, buttons)))


def test_a_selection_outside_the_bounded_list_is_rejected(monkeypatch):
    monkeypatch.setattr(history, "get_self_client", lambda: _Client([_user()]))
    result = _run(history._pick_chat("99999"))
    assert "no longer in the accessible list" in result[1]
    assert history._STATE.chat_id == 0


def test_pagination_is_bounded_to_the_scanned_page(monkeypatch):
    dialogs = [
        _dialog(SimpleNamespace(id=1000 + i, first_name=f"User{i}"), name=f"User{i}", user=True)
        for i in range(20)
    ]
    monkeypatch.setattr(history, "get_self_client", lambda: _Client(dialogs))
    _title, _body, first = _run(history._panel_handler(None, "chats:0"))
    datas = _buttons((_title, _body, first))
    assert "panel:history:chats:1" in datas
    _title, body, second = _run(history._panel_handler(None, "chats:1"))
    assert "page 2" in body
    labels = " ".join(_labels((_title, body, second)))
    assert "User8" in labels and "User0" not in labels


# ── count and focus ────────────────────────────────────────────────────────

def test_count_accepts_only_the_offered_values():
    _run(history._count_action(None, "500", PANEL_CHAT))
    assert history._STATE.count == 500
    _run(history._count_action(None, "999", PANEL_CHAT))
    assert history._STATE.count == 500
    # The established 1,000-message bound is authoritative; 1,001 is refused.
    _run(history._count_action(None, "1001", PANEL_CHAT))
    assert history._STATE.count == 500
    assert max(history.COUNTS) == history.history_service.MAX_HISTORY_MESSAGES


def test_focus_is_normalized_and_limited(monkeypatch):
    client = _Client()
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    _run(
        history._focus_input_handler(
            "  be   concise ", CHAT, USER_MSG, CHAT, PANEL_MSG, INLINE_ID,
        )
    )
    assert history._STATE.focus == "be concise"
    assert client.deleted == [(CHAT, [USER_MSG])]
    assert "Focus: be concise" in _helper.last_text

    before = history._STATE.focus
    _run(
        history._focus_input_handler(
            "x" * (history.history_ai_service.MAX_INSTRUCTION_CHARS + 1),
            CHAT, USER_MSG, CHAT, PANEL_MSG, INLINE_ID,
        )
    )
    assert history._STATE.focus == before
    assert "characters or fewer" in _helper.last_text


# ── manual summarization ───────────────────────────────────────────────────

def _capture_task(monkeypatch):
    tasks: list = []

    def _start(coro, name=""):
        # Capture the coroutine, not an asyncio future: the test helper runs
        # each action in its own short-lived loop, and a future may not cross
        # event loops.
        tasks.append(coro)
        return None

    monkeypatch.setattr(history, "guarded_create_task", _start)
    return tasks


def test_manual_run_passes_the_selected_chat_count_focus_and_no_current_message(
    monkeypatch,
):
    client = _Client([_user(1234, "Alice")])
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(
        history, "_list_chats",
        lambda _client: _async(([_chat(1234, "Alice")], "")),
    )
    manager = object()
    monkeypatch.setattr(history, "_manager", lambda: manager)
    calls: list[dict] = []

    async def _summarize(source, chat_id, **kwargs):
        calls.append({"source": source, "chat_id": chat_id, **kwargs})
        return True, "A short summary.", {
            "requested": kwargs["count"], "processed": 2, "truncated": False,
            "capped": False, "first_message_id": 10, "last_message_id": 11,
        }

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summarize)
    tasks = _capture_task(monkeypatch)
    history._STATE.chat_id = 1234
    history._STATE.chat_name = "Alice"
    history._STATE.count = 250
    history._STATE.focus = "focus here"

    assert _run(history._run_action(_event(), "", PANEL_CHAT)) is None
    assert len(tasks) == 1
    _run(tasks[0])

    call = calls[0]
    assert call["source"] is client
    assert call["chat_id"] == 1234
    assert call["count"] == 250
    assert call["instruction"] == "focus here"
    assert call["provider_manager"] is manager
    assert call["current_message_id"] is None
    assert "Processed: 2 of 250 requested" in _helper.last_text
    assert "Range: #10 – #11" in _helper.last_text
    assert "A short summary." in _helper.last_text
    # The Helper Bot performed the edit against the inline message it owns.
    assert _helper.edits[-1][0] is INLINE_ID
    assert client.edits == []


def test_manual_run_requires_an_accessible_selected_chat(monkeypatch):
    client = _Client([])
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(history, "_list_chats", lambda _c: _async(([], "")))

    result = _run(history._run_action(_event(), "", PANEL_CHAT))
    assert "Select a chat first" in result[1]


def test_provider_failure_is_rendered_as_failure_not_a_summary(monkeypatch):
    _wire_manual(monkeypatch)

    async def _fail(source, chat_id, **kwargs):
        return False, "provider exploded", {"error": "ai_provider_failed"}

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _fail)
    tasks = _capture_task(monkeypatch)
    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])
    assert "provider exploded" in _helper.last_text
    assert "Processed:" not in _helper.last_text


def test_empty_history_does_not_fabricate_a_summary(monkeypatch):
    _wire_manual(monkeypatch)

    async def _empty(source, chat_id, **kwargs):
        return True, "There are no messages to summarize in this chat.", {
            "requested": 100, "processed": 0, "truncated": False, "capped": False,
            "first_message_id": None, "last_message_id": None,
        }

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _empty)
    tasks = _capture_task(monkeypatch)
    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])
    assert "History: empty." in _helper.last_text
    assert "There are no messages to summarize" in _helper.last_text


class _ClientHolder:
    value = None

    @property
    def last_text(self) -> str:
        return self.value.last_text if self.value is not None else ""


_client = _ClientHolder()


def _wire_manual(monkeypatch):
    client = _Client([_user(1234, "Alice")])
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(
        history, "_list_chats", lambda _c: _async(([_chat(1234, "Alice")], "")),
    )
    monkeypatch.setattr(history, "_manager", lambda: object())
    history._STATE.chat_id = 1234
    history._STATE.chat_name = "Alice"


def _async(value):
    async def _coro():
        return value

    return _coro()


# ── timeout / stale writer ─────────────────────────────────────────────────

def test_the_operation_has_an_explicit_timeout(monkeypatch):
    _wire_manual(monkeypatch)

    async def _slow(source, chat_id, **kwargs):
        await asyncio.sleep(0.2)
        return True, "late summary", {"processed": 1}

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _slow)
    monkeypatch.setattr(history, "OPERATION_TIMEOUT_S", 0.01)
    tasks = _capture_task(monkeypatch)
    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])
    assert "timed out" in _helper.last_text
    assert "late summary" not in _helper.last_text


def test_cancellation_propagates_without_a_terminal_fabrication(monkeypatch):
    client = _Client()
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)

    async def _slow(source, chat_id, **kwargs):
        await asyncio.sleep(10)
        return True, "should not render", {}

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _slow)

    async def _scenario():
        gen = history.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
        task = asyncio.ensure_future(
            history._run_summary(
                client, object(), chat_id=CHAT, chat_name="Alice", count=100,
                focus="", panel_chat_id=PANEL_CHAT, panel_msg_id=PANEL_MSG,
                inline_id=INLINE_ID, generation=gen,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(_scenario())
    assert "should not render" not in _helper.last_text


def test_the_panel_provider_prompt_omits_ids_and_sender_metadata():
    from backend.services import history_ai_service

    messages = [
        SimpleNamespace(
            id=11, chat_id=CHAT, sender_id=OWNER, text="hello one",
            date=None, media=None, out=False, reply_to=None,
        ),
        SimpleNamespace(
            id=12, chat_id=CHAT, sender_id=OWNER, text="hello two",
            date=None, media=None, out=False, reply_to=None,
        ),
    ]
    client = _HistoryClient(messages)

    class _Manager:
        def __init__(self):
            self.prompts = []

        async def chat(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return SimpleNamespace(success=True, text="summary", metadata={})

    manager = _Manager()
    ok, text, _data = _run(
        history_ai_service.summarize_history(
            client, CHAT, count=2, provider_manager=manager,
            with_identifiers=False,
        )
    )
    assert ok and text == "summary"
    payload = manager.prompts[0][-1]["content"]
    assert "hello one" in payload and "hello two" in payload
    assert "[11]" not in payload and "[12]" not in payload
    assert "User " not in payload
    assert str(OWNER) not in payload


def test_a_superseded_operation_cannot_overwrite_the_newer_panel(monkeypatch):
    client = _Client()
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(history, "_manager", lambda: object())

    async def _summary(source, chat_id, **kwargs):
        return True, "stale", {"processed": 1, "requested": 1,
                               "first_message_id": 1, "last_message_id": 1}

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summary)
    generation = history.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
    supersede_panel_operation(PANEL_CHAT, PANEL_MSG)
    _run(
        history._run_summary(
            client, object(), chat_id=CHAT, chat_name="Alice", count=100,
            focus="", panel_chat_id=PANEL_CHAT, panel_msg_id=PANEL_MSG,
            inline_id=INLINE_ID, generation=generation,
        )
    )
    assert _helper.edits == []
    assert client.edits == []


# ── links ─────────────────────────────────────────────────────────────────

def _wire_link(monkeypatch, message):
    client = _Client()
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(history, "_manager", lambda: object())
    calls: list[dict] = []

    async def _fetch(source, target):
        return message

    async def _summary(source, chat_id, **kwargs):
        calls.append({"chat_id": chat_id, **kwargs})
        return True, "link summary", {
            "requested": kwargs["count"], "processed": 3, "truncated": False,
            "capped": False,
            "first_message_id": target_id - 2, "last_message_id": target_id,
        }

    target_id = message.id
    monkeypatch.setattr(history.save_service, "_fetch_linked_message", _fetch)
    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summary)
    tasks = _capture_task(monkeypatch)
    return client, calls, tasks


def test_link_run_uses_linked_id_plus_one_as_the_exclusive_boundary(monkeypatch):
    linked = 500
    message = SimpleNamespace(
        id=linked, chat_id=CHAT,
        chat=SimpleNamespace(id=555, title="News", megagroup=True),
    )
    client, calls, tasks = _wire_link(monkeypatch, message)
    history._STATE.count = 250
    history._STATE.focus = "summarize decisions"

    _run(
        history._link_input_handler(
            "https://t.me/news/500", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])

    assert calls[0]["chat_id"] == CHAT
    assert calls[0]["count"] == 250
    assert calls[0]["instruction"] == "summarize decisions"
    assert calls[0]["current_message_id"] == linked + 1
    assert "Ending at: #500" in _helper.last_text
    assert "Range: #498 – #500" in _helper.last_text
    assert client.deleted == [(CHAT, [USER_MSG])]


def test_private_link_form_resolves_and_summarizes(monkeypatch):
    message = SimpleNamespace(id=42, chat_id=-1003080318802, chat=None)
    client, calls, tasks = _wire_link(monkeypatch, message)
    _run(
        history._link_input_handler(
            "https://t.me/c/3080318802/42", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])
    assert calls[0]["chat_id"] == -1003080318802
    assert calls[0]["current_message_id"] == 43


def test_tg_openmessage_form_resolves_and_summarizes(monkeypatch):
    message = SimpleNamespace(id=209974, chat_id=5406707941, chat=None)
    _client.value = _Client()
    monkeypatch.setattr(history, "get_self_client", lambda: _client.value)
    monkeypatch.setattr(history, "_manager", lambda: object())
    monkeypatch.setattr(
        history.save_service, "_fetch_linked_message",
        lambda source, target: _async(message),
    )
    calls = []

    async def _summary(source, chat_id, **kwargs):
        calls.append((chat_id, kwargs.get("current_message_id")))
        return True, "ok", {"processed": 1, "requested": 100}

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summary)
    tasks = _capture_task(monkeypatch)
    _run(
        history._link_input_handler(
            "tg://openmessage?user_id=5406707941&message_id=209974",
            CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG, INLINE_ID,
        )
    )
    _run(tasks[0])
    assert calls == [(5406707941, 209975)]


def test_an_invalid_link_is_rejected_before_any_resolution_or_ai(monkeypatch):
    client = _Client()
    monkeypatch.setattr(history, "get_self_client", lambda: client)

    async def _must_not_fetch(*args, **kwargs):
        raise AssertionError("resolver must not run for an invalid link")

    monkeypatch.setattr(history.save_service, "_fetch_linked_message", _must_not_fetch)
    _run(
        history._link_input_handler(
            "not a telegram link", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    assert "not a Telegram message link" in _helper.last_text
    assert client.deleted == [(CHAT, [USER_MSG])]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (save_service.LINK_PEER_UNRESOLVED, "Could not reach the chat"),
        (save_service.LINK_MESSAGE_MISSING, "Message not found"),
        (save_service.LINK_LOOKUP_FAILED, "Could not read the linked message"),
    ],
)
def test_link_failure_kinds_are_distinct(monkeypatch, kind, expected):
    client = _Client()
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(history, "_manager", lambda: object())

    async def _fail(source, target):
        raise save_service.LinkResolutionError(kind, "detail")

    monkeypatch.setattr(history.save_service, "_fetch_linked_message", _fail)
    tasks = _capture_task(monkeypatch)
    _run(
        history._link_input_handler(
            "https://t.me/news/500", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])
    assert expected in _helper.last_text


def test_a_peer_or_message_mismatch_is_refused(monkeypatch):
    wrong_chat = SimpleNamespace(id=500, chat_id=-100999, chat=None)
    client, _calls, tasks = _wire_link(monkeypatch, wrong_chat)
    _run(
        history._link_input_handler(
            "https://t.me/c/3080318802/500", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])
    assert "requested chat" in _helper.last_text

    wrong_id = SimpleNamespace(id=499, chat_id=-1003080318802, chat=None)
    client, _calls, tasks = _wire_link(monkeypatch, wrong_id)
    _run(
        history._link_input_handler(
            "https://t.me/c/3080318802/500", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])
    assert "requested message" in _helper.last_text


def test_no_permanent_worker_or_loop_is_introduced():
    source = open(history.__file__, encoding="utf-8").read()
    assert "while True" not in source
    assert "TaskScheduler" not in source
    assert "new_task_scheduler" not in source


# ── link boundary at the history-service level ─────────────────────────────


class _HistoryClient:
    def __init__(self, messages):
        self.messages = messages
        self.max_ids = []

    def iter_messages(self, chat_id, **kwargs):
        self.max_ids.append(kwargs.get("max_id"))
        max_id = kwargs.get("max_id")
        selected = [m for m in self.messages if max_id is None or m.id < max_id]
        selected.sort(key=lambda m: m.id, reverse=True)
        limit = kwargs.get("limit")
        if limit is not None:
            selected = selected[:limit]

        async def _aiter():
            for item in selected:
                yield item

        return _aiter()


def test_the_exclusive_boundary_includes_the_linked_message_and_excludes_newer():
    from backend.services import history_service

    messages = [
        SimpleNamespace(id=i, chat_id=CHAT, text=f"m{i}", sender_id=OWNER)
        for i in range(1, 21)
    ]
    client = _HistoryClient(messages)
    slice_ = _run(
        history_service.fetch_recent_history(
            client, CHAT, count=5, before_id=13,
        )
    )
    assert [m.message_id for m in slice_.messages] == [8, 9, 10, 11, 12]
    assert 13 not in [m.message_id for m in slice_.messages]
    assert client.max_ids[0] == 13


# ── result delivery: the Helper Bot edits the ORIGINAL panel ────────────────


class _Lifecycle:
    def __init__(self):
        self.calls: list[tuple[int, int, str, str]] = []

    async def replace_panel(self, chat_id, msg_id, panel_id, extra=""):
        self.calls.append((chat_id, msg_id, panel_id, extra))
        return True


def _patch_lifecycle(monkeypatch, lifecycle):
    """Make ANY lifecycle replacement in this test visible (it must be none)."""
    monkeypatch.setattr(lifecycle_module, "get_lifecycle", lambda: lifecycle)


def _wire_delivery(monkeypatch, summary, *, media=True):
    client = _Client([_user(1234, "Alice")])
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(
        history, "_list_chats", lambda _c: _async(([_chat(1234, "Alice")], "")),
    )
    monkeypatch.setattr(history, "_manager", lambda: object())

    async def _summarize(source, chat_id, **kwargs):
        return True, summary, {
            "requested": kwargs["count"], "processed": 5, "truncated": False,
            "capped": False, "first_message_id": 1, "last_message_id": 5,
        }

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summarize)
    history._STATE.chat_id = 1234
    history._STATE.chat_name = "Alice"
    if media:
        appearance_service.note_delivered(PANEL_CHAT, PANEL_MSG, ("media", "v1"))
    return client


def _summary_run(client, *, inline_id=INLINE_ID):
    generation = history.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
    _run(
        history._run_summary(
            client, object(), chat_id=CHAT, chat_name="Alice", count=100,
            focus="", panel_chat_id=PANEL_CHAT, panel_msg_id=PANEL_MSG,
            inline_id=inline_id, generation=generation,
        )
    )
    return generation


def test_the_result_edits_the_original_panel_through_the_helper_bot(monkeypatch):
    client = _wire_delivery(monkeypatch, "A short summary.", media=False)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)

    _summary_run(client)

    assert "A short summary." in _helper.last_text
    assert "Range: #1 – #5" in _helper.last_text
    # The edit addresses the exact inline message the callback belongs to.
    assert _helper.edits[-1][0] is INLINE_ID
    # The owner's self client never edits the panel ...
    assert client.edits == []
    # ... and nothing is replaced or created anywhere.
    assert lifecycle.calls == []


def test_the_helper_bot_is_the_only_client_that_edits_the_panel(monkeypatch):
    client = _wire_delivery(monkeypatch, "short", media=False)
    _summary_run(client)

    assert _helper.edits, "the Helper Bot must perform the edits"
    assert client.edits == []


def test_a_long_media_summary_is_bounded_into_the_same_panel(monkeypatch):
    summary = "Summary " + "x" * 1500
    client = _wire_delivery(monkeypatch, summary, media=True)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)

    _summary_run(client)

    text = _helper.last_text
    assert summary in text or text.startswith("**History & Intelligence**")
    assert len(text) <= appearance_service.MAX_CAPTION_CHARS
    assert "bounded to this panel's Telegram limit" in text
    assert summary[:200] in text
    assert _helper.edits[-1][0] is INLINE_ID
    assert client.edits == []
    assert lifecycle.calls == []


def test_a_long_text_panel_summary_keeps_the_full_text(monkeypatch):
    summary = "Summary " + "y" * 1500
    client = _wire_delivery(monkeypatch, summary, media=False)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)

    _summary_run(client)

    text = _helper.last_text
    assert summary in text
    assert len(text) <= history.MAX_RESULT_TEXT_CHARS
    assert "bounded" not in text
    assert lifecycle.calls == []


def test_a_permission_refusal_is_classified_and_never_a_success(
    monkeypatch, caplog,
):
    client = _wire_delivery(monkeypatch, "short summary", media=False)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)
    _helper.error = type("ChatAdminRequiredError", (Exception,), {})("no rights")

    with caplog.at_level(logging.WARNING):
        _summary_run(client)

    assert "HISTORY_RESULT_DELIVERY_FAILED" in caplog.text
    assert "reason=helper_panel_edit_failed" in caplog.text
    assert "error=ChatAdminRequiredError" in caplog.text
    assert "kind=permission" in caplog.text
    assert "HISTORY_RESULT_DELIVERED" not in caplog.text
    assert lifecycle.calls == []
    assert client.edits == []


def test_an_invalid_or_deleted_panel_never_creates_a_replacement(
    monkeypatch, caplog,
):
    client = _wire_delivery(monkeypatch, "short summary", media=False)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)
    _helper.error = type("MessageIdInvalidError", (Exception,), {})("gone")

    with caplog.at_level(logging.WARNING):
        _summary_run(client)

    assert "kind=invalid_reference" in caplog.text
    assert "HISTORY_RESULT_DELIVERED" not in caplog.text
    assert lifecycle.calls == []
    assert client.edits == []


def test_no_inline_handle_fails_honestly_without_any_fallback(
    monkeypatch, caplog,
):
    client = _wire_delivery(monkeypatch, "short summary", media=False)
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)

    with caplog.at_level(logging.WARNING):
        _summary_run(client, inline_id=None)

    assert "kind=no_inline_panel_handle" in caplog.text
    assert "HISTORY_RESULT_DELIVERED" not in caplog.text
    assert _helper.edits == []
    assert client.edits == []
    assert lifecycle.calls == []


def test_delivery_text_bounds_media_and_text_panels_differently():
    body = "z" * 4000
    media_text = history._delivery_text(body, media_panel=True)
    plain_text = history._delivery_text(body, media_panel=False)
    assert len(media_text) <= appearance_service.MAX_CAPTION_CHARS
    assert media_text.endswith("Telegram limit)")
    assert len(plain_text) <= history.MAX_RESULT_TEXT_CHARS
    assert history._delivery_text(body, media_panel=False) == plain_text


def test_no_saved_messages_delivery_path_exists_in_history():
    source = open(history.__file__, encoding="utf-8").read()
    assert "Saved Messages" not in source
    assert "replace_panel" not in source
    assert "get_lifecycle" not in source
    assert "destination_not_owner_private" not in source
    assert "no_safe_text_path" not in source
    assert "_RESULT_VIEWS" not in source
    # No second Telegram client / lifecycle is created by this handler.
    assert "TelegramClient(" not in source
    assert "build_helper" not in source


def test_a_focused_long_summary_uses_the_corrected_delivery(monkeypatch):
    summary = "Focused " + "f" * 1400
    client, calls = _wire_focus(monkeypatch, summary)
    history._STATE.focus = "decisions only"
    appearance_service.note_delivered(PANEL_CHAT, PANEL_MSG, ("media", "v1"))
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)
    tasks = _capture_task(monkeypatch)

    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])

    assert calls[0]["instruction"] == "decisions only"
    assert summary[:200] in _helper.last_text
    assert len(_helper.last_text) <= appearance_service.MAX_CAPTION_CHARS
    assert client.edits == []
    assert lifecycle.calls == []


def test_a_long_link_summary_uses_the_corrected_delivery(monkeypatch):
    linked = 500
    message = SimpleNamespace(
        id=linked, chat_id=CHAT,
        chat=SimpleNamespace(id=555, title="News", megagroup=True),
    )
    client = _Client()
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(history, "_manager", lambda: object())
    calls: list[dict] = []
    summary = "Linked " + "l" * 1400

    async def _fetch(source, target):
        return message

    async def _summary(source, chat_id, **kwargs):
        calls.append({"chat_id": chat_id, **kwargs})
        return True, summary, {
            "requested": kwargs["count"], "processed": 3, "truncated": False,
            "capped": False, "first_message_id": linked - 2, "last_message_id": linked,
        }

    monkeypatch.setattr(history.save_service, "_fetch_linked_message", _fetch)
    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summary)
    appearance_service.note_delivered(PANEL_CHAT, PANEL_MSG, ("media", "v1"))
    lifecycle = _Lifecycle()
    _patch_lifecycle(monkeypatch, lifecycle)
    tasks = _capture_task(monkeypatch)

    _run(
        history._link_input_handler(
            "https://t.me/news/500", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG,
            INLINE_ID,
        )
    )
    _run(tasks[0])

    assert calls[0]["current_message_id"] == linked + 1
    assert summary[:200] in _helper.last_text
    assert "Ending at: #500" in _helper.last_text
    assert len(_helper.last_text) <= appearance_service.MAX_CAPTION_CHARS
    assert client.edits == []
    assert lifecycle.calls == []


def _wire_focus(monkeypatch, summary):
    calls: list[dict] = []
    client = _Client([_user(1234, "Alice")])
    _client.value = client
    monkeypatch.setattr(history, "get_self_client", lambda: client)
    monkeypatch.setattr(
        history, "_list_chats", lambda _c: _async(([_chat(1234, "Alice")], "")),
    )
    monkeypatch.setattr(history, "_manager", lambda: object())

    async def _summarize(source, chat_id, **kwargs):
        calls.append(dict(kwargs))
        return True, summary, {
            "requested": kwargs["count"], "processed": 3, "truncated": False,
            "capped": False, "first_message_id": 1, "last_message_id": 3,
        }

    monkeypatch.setattr(history.history_ai_service, "summarize_history", _summarize)
    history._STATE.chat_id = 1234
    history._STATE.chat_name = "Alice"
    return client, calls


def test_a_valid_focus_instruction_is_saved_and_reaches_the_summarizer(monkeypatch):
    client, calls = _wire_focus(monkeypatch, "focused result")
    tasks = _capture_task(monkeypatch)

    _run(
        history._focus_input_handler(
            "  keep   decisions ", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG, INLINE_ID,
        )
    )
    assert history._STATE.focus == "keep decisions"

    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])

    assert calls[0]["instruction"] == "keep decisions"
    assert "focused result" in _helper.last_text


def test_an_empty_focus_instruction_clears_the_previous_value(monkeypatch):
    client, calls = _wire_focus(monkeypatch, "unfocused result")
    history._STATE.focus = "old focus"
    tasks = _capture_task(monkeypatch)

    _run(
        history._focus_input_handler(
            "   ", CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG, INLINE_ID,
        )
    )
    assert history._STATE.focus == ""

    _run(history._run_action(_event(), "", PANEL_CHAT))
    _run(tasks[0])
    assert calls[0]["instruction"] is None


def test_an_over_limit_focus_is_rejected_without_replacing_the_previous_value(monkeypatch):
    client, _calls = _wire_focus(monkeypatch, "unused")
    history._STATE.focus = "previous"
    too_long = "x" * (history.history_ai_service.MAX_INSTRUCTION_CHARS + 1)

    _run(
        history._focus_input_handler(
            too_long, CHAT, USER_MSG, PANEL_CHAT, PANEL_MSG, INLINE_ID,
        )
    )

    assert history._STATE.focus == "previous"
    assert "characters or fewer" in _helper.last_text
