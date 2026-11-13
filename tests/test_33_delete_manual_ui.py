"""Manual Delete Glass UI — real callback/input lifecycle coverage.

Every test drives production functions: the registered panel handlers, the
registered input configurations through ``panels._handle_input``, the real
``inline_sender`` input listener, and the real ``delete_service``. Only the
Telegram client and the panel transport are fakes.

The properties locked in here are the reported defects:

  - the manual panel works from its OWN chat context, never a reply
    ``TargetContext`` (which expires and rejects ordinary panel use);
  - the numeric input executes the requested deletion, above 500 too;
  - Recent Messages paginates, previews, and distinguishes deleting ONE
    selected message from deleting from it FORWARD;
  - the manual "All my messages" option is FROZEN: the button is gone, a
    stale panel can show neither the preview nor the confirmation, and a
    replayed "allexec" callback can never start the deletion (the backend
    capability is untouched — the AI delete tool still covers it);
  - every manual path still reaches the single ownership chokepoint.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import InputBotInlineMessageID

from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteRepliedTool, DeleteTool
from backend.ai.tools.semantic import DeleteMessagesByIdsTool, ListRecentMessagesTool
from backend.bot.handlers import delete as delete_ui
from backend.helper import callback_gate, inline_engine, input_state, panels, target_context
from backend.helper.inline_sender import register_input_listener
from backend.services import delete_service

OWNER = 111
CHAT = -100
PANEL_MSG = 500
COMMAND_MSG = 900
INLINE_ID = InputBotInlineMessageID(dc_id=2, id=1, access_hash=2)

delete_ui.register(None, OWNER)


# ── fakes ───────────────────────────────────────────────────────────────────


class Message:
    def __init__(self, mid, *, out=True, sender_id=OWNER, text="", date=None):
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.date = date
        self.media = None
        self.reply_to_msg_id = None


class FakeClient:
    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}
        self.me = SimpleNamespace(id=OWNER)
        self.deleted: list[int] = []
        self.batches: list[list[int]] = []
        self.edits: list[tuple[int, int, str]] = []
        #: Panel edits attempted with the owner's OWN account. The Glass panel
        #: belongs to the Helper Bot, so tests assert this stays empty.
        self.self_edits: list[tuple] = []
        self.handlers: list = []
        self.fail_on_batch: int | None = None
        self.delete_delay = 0.0

    def on(self, event):
        def decorator(function):
            self.handlers.append(function)
            return function

        return decorator

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit")
        from_user = kwargs.get("from_user")
        min_id = kwargs.get("min_id")
        emitted = 0
        for mid in sorted(self.messages, reverse=True):
            message = self.messages[mid]
            if min_id is not None and mid <= min_id:
                continue
            if from_user == "me" and not message.out:
                continue
            yield message
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(mid) for mid in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        batch = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        if self.delete_delay:
            await asyncio.sleep(self.delete_delay)
        self.batches.append(batch)
        if self.fail_on_batch is not None and len(self.batches) == self.fail_on_batch:
            raise RuntimeError("Telegram RPC failed")
        for mid in batch:
            self.messages.pop(mid, None)
        self.deleted.extend(batch)

    async def edit_message(self, chat_id, message_id, text, **kwargs):
        self.self_edits.append((chat_id, message_id, text))


class FakeHelper:
    """The Helper Bot: the ONLY principal allowed to edit the Glass panel."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.inline_ids: list = []
        self.edits: list[tuple] = []

    async def edit_message(self, inline_id, text=None, buttons=None, **kwargs):
        self.inline_ids.append(inline_id)
        if self._client.fail_edits:
            raise RuntimeError("panel edit refused")
        self.edits.append((inline_id, text))
        self._client.edits.append((None, None, text))


class FakeEvent:
    def __init__(self, *, chat_id=CHAT, message_id=PANEL_MSG, text="", sender_id=OWNER,
                 msg_id=None, inline_identifier=INLINE_ID):
        self.chat_id = chat_id
        self.message_id = message_id
        self.raw_text = text
        self.text = text
        self.sender_id = sender_id
        self.out = True
        self.id = msg_id if msg_id is not None else message_id
        self.message = SimpleNamespace(id=self.id)
        self.inline_message_id = None
        # A callback on an inline panel carries Telegram's identifier here.
        self.query = SimpleNamespace(msg_id=inline_identifier)
        self.edits: list[str] = []

    async def edit(self, text, buttons=None, **kwargs):
        self.edits.append(text)


def install(monkeypatch, messages) -> FakeClient:
    client = FakeClient(messages)
    client.fail_edits = False
    client.helper = FakeHelper(client)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(delete_ui, "get_client", lambda: client.helper)
    return client


def button_data(buttons) -> list[str]:
    out: list[str] = []
    for row in buttons:
        for button in row:
            data = getattr(button, "data", b"")
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


@pytest.fixture(autouse=True)
def _isolated():
    input_state.clear_all()
    target_context.clear_all()
    for key in list(callback_gate._painters):
        callback_gate.forget_painter(*key)
    yield
    input_state.clear_all()
    target_context.clear_all()


def context(client: FakeClient, *, chat_id: int = CHAT) -> ToolContext:
    return ToolContext(
        client=client,
        telegram=SimpleNamespace(client=client),
        owner_id=OWNER,
        tz_str="UTC",
        extra={"chat_id": chat_id, "request_id": "req-manual"},
    )


# ── 1/2. numeric input: real callback arming → real listener ────────────────


@pytest.mark.asyncio
async def test_numeric_input_executes_the_expected_deletion(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 11)])
    register_input_listener(client, OWNER)

    await panels._handle_input(FakeEvent(message_id=PANEL_MSG), "del:n", OWNER, CHAT, PANEL_MSG)
    assert input_state.get_pending(OWNER) is not None

    await client.handlers[0](FakeEvent(message_id=COMMAND_MSG, text="3"))

    # Only the three requested messages are the operation's deletions; the
    # typed command message is removed afterwards as UI cleanup (never counted).
    assert [mid for mid in client.deleted if mid != COMMAND_MSG] == [10, 9, 8]
    assert input_state.get_pending(OWNER) is None
    assert client.edits and "Deleted 3" in client.edits[-1][2]
    assert COMMAND_MSG in client.deleted


@pytest.mark.asyncio
async def test_numeric_input_supports_values_above_500(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 751)])
    register_input_listener(client, OWNER)
    await panels._handle_input(FakeEvent(message_id=PANEL_MSG), "del:n", OWNER, CHAT, PANEL_MSG)

    await client.handlers[0](FakeEvent(message_id=COMMAND_MSG, text="750"))

    operation = [mid for mid in client.deleted if mid != COMMAND_MSG]
    assert len(operation) == 750
    assert len(set(operation)) == 750
    assert "Deleted 750" in client.edits[-1][2]


# ── 3/4. Recent Messages: no reply context, previews, pagination ────────────


@pytest.mark.asyncio
async def test_recent_messages_work_without_reply_target_context(monkeypatch):
    client = install(monkeypatch, [Message(i, text=f"note {i}") for i in range(1, 11)])
    target_context.clear_all()

    _title, body, buttons = await delete_ui._delfrom_panel_handler(FakeEvent(), "recent:0")

    assert "No chat context" not in body
    picks = [data for data in button_data(buttons) if data.startswith("panel:delfrom:pick:")]
    assert len(picks) == 8  # one page of the newest self-owned messages
    assert client.batches == []


@pytest.mark.asyncio
async def test_recent_messages_paginate(monkeypatch):
    install(monkeypatch, [Message(i, text=f"note {i}") for i in range(1, 21)])

    _title, body0, buttons0 = await delete_ui._delfrom_panel_handler(FakeEvent(), "recent:0")
    _title, body1, buttons1 = await delete_ui._delfrom_panel_handler(FakeEvent(), "recent:1")

    page0 = [d for d in button_data(buttons0) if d.startswith("panel:delfrom:pick:")]
    page1 = [d for d in button_data(buttons1) if d.startswith("panel:delfrom:pick:")]
    assert page0 and page1
    assert set(page0).isdisjoint(page1)
    assert any(d.startswith("panel:delfrom:recent:1") for d in button_data(buttons0))
    assert any(d.startswith("panel:delfrom:recent:0") for d in button_data(buttons1))
    assert "page 1" in body0 and "page 2" in body1


@pytest.mark.asyncio
async def test_recent_page_requires_the_chat_the_panel_lives_in(monkeypatch):
    install(monkeypatch, [Message(i) for i in range(1, 5)])
    event = FakeEvent()
    event.chat_id = None  # an inline panel with no resolvable session
    _title, body, _buttons = await delete_ui._delfrom_panel_handler(event, "recent:0")
    assert "Could not determine this chat" in body


# ── 5/6. one selected message vs from-that-message forward ──────────────────


@pytest.mark.asyncio
async def test_selecting_single_message_deletes_only_that_message(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 11)])

    _title, body, _buttons = await delete_ui._delfrom_panel_handler(
        FakeEvent(), f"oneexec:{7}"
    )

    assert client.deleted == [7]
    assert "Deleted 1" in body


@pytest.mark.asyncio
async def test_selecting_boundary_deletes_only_the_intended_self_range(monkeypatch):
    client = install(
        monkeypatch,
        [
            Message(10), Message(9, out=False, sender_id=222),
            Message(8), Message(7, out=False, sender_id=222),
            Message(6), Message(5), Message(4),
        ],
    )

    _title, _body, _buttons = await delete_ui._delfrom_panel_handler(
        FakeEvent(), "fromexec:6"
    )

    # "Forward" means every one of the owner's messages at/after that message
    # in time (higher ids); older messages are outside the requested range.
    assert sorted(client.deleted) == [6, 8, 10]
    assert 4 not in client.deleted and 5 not in client.deleted
    assert 7 not in client.deleted and 9 not in client.deleted  # foreign


# ── 7. manual Message ID without any reply context ──────────────────────────


@pytest.mark.asyncio
async def test_manual_message_id_input_works_without_reply_context(monkeypatch):
    client = install(monkeypatch, [Message(42), Message(43, out=False, sender_id=222)])
    target_context.clear_all()

    await delete_ui._del_id_input_handler(
        "42", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )

    assert [mid for mid in client.deleted if mid != COMMAND_MSG] == [42]
    assert "Deleted 1" in client.edits[-1][2]


@pytest.mark.asyncio
async def test_manual_message_id_input_rejects_foreign_and_malformed_ids(monkeypatch):
    client = install(monkeypatch, [Message(42, out=False, sender_id=222)])

    await delete_ui._del_id_input_handler(
        "42", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )
    await delete_ui._del_id_input_handler(
        "abc", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )

    assert [mid for mid in client.deleted if mid != COMMAND_MSG] == []
    assert "Nothing was deleted" in client.edits[0][2]
    assert "valid message ID" in client.edits[1][2]


# ── 8/9/10/11. the manual All option is frozen ────────────────────────────


@pytest.mark.asyncio
async def test_frozen_all_callback_deletes_nothing_however_many_messages(monkeypatch):
    # Even a 1,320-message chat deletes nothing: the manual All option is
    # frozen, so an old panel can neither scan, confirm nor delete.
    client = install(monkeypatch, [Message(i) for i in range(1, 1321)])
    client.fail_on_batch = 2

    for extra in ("all", "allexec"):
        title, body, buttons = await delete_ui._del_panel_handler(FakeEvent(), extra)

        assert client.deleted == []
        assert "temporarily unavailable" in body
        assert "Nothing was deleted" in body
        assert "Completed" not in body
        assert "panel:del:allexec" not in button_data(buttons)
        assert "panel:del" in button_data(buttons)  # a way back, no confirmation
        assert title == "Delete"


@pytest.mark.asyncio
async def test_frozen_all_callbacks_never_invoke_the_deletion_service(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 51)])
    calls: list[str] = []

    async def _recorder(name):
        async def _inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must never run from a frozen callback")
        return _inner

    for name in ("do_del_self_filtered", "do_del_n_counts", "preview_self_owned_count"):
        monkeypatch.setattr(delete_service, name, await _recorder(name))

    for extra in ("all", "allexec"):
        await delete_ui._del_panel_handler(FakeEvent(), extra)

    assert calls == []
    assert client.deleted == []


@pytest.mark.asyncio
async def test_frozen_all_callbacks_show_no_preview_or_confirmation(monkeypatch):
    install(monkeypatch, [Message(i) for i in range(1, 31)])

    _title, body, buttons = await delete_ui._del_panel_handler(FakeEvent(), "all")

    assert "Preview:" not in body
    assert "Confirm" not in body
    assert "Delete ALL" not in body
    data = button_data(buttons)
    assert "panel:del:allexec" not in data
    assert "panel:del" in data


# ── 12/13. timeouts: no duplicates, no false completion ────────────────────


@pytest.mark.asyncio
async def test_timeout_does_not_duplicate_deletion_or_claim_completion(monkeypatch):
    from backend.ai.tools import delete as delete_tool_module

    client = install(monkeypatch, [Message(i) for i in range(1, 301)])
    client.delete_delay = 0.04
    monkeypatch.setattr(delete_tool_module, "_DELETE_OPERATION_TIMEOUT_SECONDS", 0.05)

    ctx = context(client)
    result = await DeleteTool(ctx).execute(ctx, {"mode": "all"})

    assert result.success is False
    assert "timed out" in result.message.lower()
    assert result.data["timeout"] is True
    assert result.data["partial"] is True
    # The batch that completed is reported, nothing is deleted twice, and the
    # operation never claims it finished.
    assert result.data["count"] == len(client.deleted)
    assert len(client.deleted) == len(set(client.deleted))
    assert "Completed" not in result.message


@pytest.mark.asyncio
async def test_operation_deadline_reports_partial_history_scan(monkeypatch):
    install(monkeypatch, [Message(i) for i in range(1, 51)])

    class SlowHistory(FakeClient):
        async def iter_messages(self, chat_id, **kwargs):
            await asyncio.sleep(0.05)
            async for message in super().iter_messages(chat_id, **kwargs):
                yield message

    client = SlowHistory([Message(i) for i in range(1, 51)])
    monkeypatch.setattr(inline_engine, "_self_client", client)
    report = delete_service.DeleteReport()
    selection = await delete_service.select_self_owned_message_ids(
        client, CHAT, deadline=0,
    )
    assert selection.complete is False
    assert selection.reason
    assert selection.ids == []
    assert report.deleted == 0


# ── 14. date range uses the configured timezone ────────────────────────────


@pytest.mark.asyncio
async def test_date_range_respects_the_configured_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tehran")
    in_range = Message(3, date=datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc))   # 09:30
    too_early = Message(2, date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc))  # 06:30
    foreign = Message(4, out=False, sender_id=222,
                      date=datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc))
    client = install(monkeypatch, [in_range, too_early, foreign])

    await delete_ui._del_range_input_handler(
        "08:00 .. 10:00", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )

    assert [mid for mid in client.deleted if mid != COMMAND_MSG] == [3]
    assert "Deleted 1" in client.edits[-1][2]


def test_split_range_does_not_parse_times_itself():
    assert delete_ui.split_range("2026-08-01 .. 2026-08-20") == ("2026-08-01", "2026-08-20")
    assert delete_ui.split_range("yesterday, today") == ("yesterday", "today")
    assert delete_ui.split_range("09:00 to 18:00") == ("09:00", "18:00")
    assert delete_ui.split_range("09:00") is None


# ── 15/16/17. semantic delete contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_ids_must_come_from_the_history_tool(monkeypatch):
    client = install(monkeypatch, [Message(1, text="hello world"), Message(2, text="keep")])
    ctx = context(client)

    listed = await ListRecentMessagesTool(ctx).execute(ctx, {"limit": 10})
    ids = [item["id"] for item in listed.data["messages"]]
    assert set(ids) == {1, 2}

    result = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": ids + [999]},
    )

    assert sorted(result.data["deleted"]) == [1, 2]
    assert 999 in result.data["rejected"]
    assert 999 not in client.deleted


@pytest.mark.asyncio
async def test_semantic_delete_supports_the_expanded_batch(monkeypatch):
    client = install(monkeypatch, [Message(i, text="hello world") for i in range(1, 301)])
    ctx = context(client)

    result = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": list(range(1, 301))},
    )

    assert result.success is True
    assert len(result.data["deleted"]) == 300
    assert len(client.deleted) == 300
    assert all(len(batch) <= 100 for batch in client.batches)


@pytest.mark.asyncio
async def test_foreign_missing_and_malformed_ids_remain_rejected(monkeypatch):
    client = install(monkeypatch, [Message(1), Message(2, out=False, sender_id=222)])
    ctx = context(client)

    result = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": [1, 2, 0, "x", 999]},
    )
    malformed = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": ["x"]},
    )

    assert result.data["deleted"] == [1]
    assert set(result.data["rejected"]) == {2, 0, "x", 999}
    assert client.deleted == [1]
    assert malformed.success is False
    assert "No valid message IDs" in malformed.message


# ── 18. the ownership chokepoint stays mandatory on every manual path ──────


@pytest.mark.asyncio
async def test_every_manual_path_reaches_the_ownership_chokepoint(monkeypatch):
    # Dates inside the range below, so the time-filter path has real
    # candidates too: a scope with zero candidates legitimately attempts no
    # deletion, and only then may it skip the chokepoint.
    in_range = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    client = install(
        monkeypatch, [Message(i, date=in_range) for i in range(1, 31)],
    )
    calls: list[list[int]] = []

    async def _spy(chosen_client, chat_id, ids, **kwargs):
        calls.append(list(ids))
        return [], list(ids)

    monkeypatch.setattr(delete_service, "delete_verified_self_messages", _spy)

    await delete_ui._del_panel_handler(FakeEvent(), "qexec:2")
    await delete_ui._delfrom_panel_handler(FakeEvent(), "oneexec:3")
    await delete_ui._delfrom_panel_handler(FakeEvent(), "fromexec:3")
    await delete_ui._del_id_input_handler(
        "4", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )
    await delete_ui._del_from_input_handler(
        "5", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )
    await delete_ui._del_n_input_handler(
        "6", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )
    await delete_ui._del_range_input_handler(
        "2026-08-01 .. 2026-08-20", CHAT, COMMAND_MSG, CHAT, PANEL_MSG,
        inline_id=INLINE_ID,
    )

    assert len(calls) == 7
    assert all(isinstance(ids, list) for ids in calls)


# ── 19/20. replied deletion + AI-generated messages ────────────────────────


@pytest.mark.asyncio
async def test_replied_message_deletion_remains_correct(monkeypatch):
    reply = Message(COMMAND_MSG)
    reply.reply_to_msg_id = 5
    client = install(monkeypatch, [Message(i) for i in range(4, 9)] + [reply])
    ctx = ToolContext(
        telegram=SimpleNamespace(client=client), owner_id=OWNER, tz_str="UTC",
        extra={"reply_msg": {"chat_id": CHAT, "message_id": 5}},
    )

    result = await DeleteRepliedTool(ctx).execute(ctx, {})
    await delete_ui._delfrom_reply_wait_handler(
        "", CHAT, COMMAND_MSG, CHAT, PANEL_MSG,
    )

    assert result.success is True
    assert 5 not in client.messages  # deleted by the tool
    assert {5, 6, 7, 8} <= set(client.deleted)
    assert 4 not in client.deleted  # older than the reply boundary


@pytest.mark.asyncio
async def test_ai_generated_self_messages_remain_eligible(monkeypatch):
    client = install(monkeypatch, [Message(11, text="Nova generated this")])

    await delete_ui._delfrom_panel_handler(FakeEvent(), "oneexec:11")

    assert client.deleted == [11]


# ── 21. the silent-success AI contract is intact ───────────────────────────


def test_ai_delete_silence_contract_is_intact():
    from backend.bot.handlers.ai_unified import _is_silent_delete

    success = SimpleNamespace(metadata={"tool_results": [
        {"tool_name": "delete", "success": True, "message": "Deleted 3."},
    ]})
    partial = SimpleNamespace(metadata={"tool_results": [
        {"tool_name": "delete", "success": False,
         "message": "Delete stopped partially: deleted 100 of 250 candidate(s)."},
    ]})
    assert _is_silent_delete(success) is True
    assert _is_silent_delete(partial) is False


# ── 22/23. the UI offers the required actions and confirms destruction ─────


@pytest.mark.asyncio
async def test_home_panel_offers_every_required_quick_action(monkeypatch):
    install(monkeypatch, [Message(1)])

    _title, _body, buttons = await delete_ui._del_panel_handler(FakeEvent(), "")
    data = button_data(buttons)

    for count in (10, 25, 50, 100, 250, 500):
        assert f"panel:del:q:{count}" in data
    assert "input:del:n" in data
    assert "panel:del:all" not in data  # frozen: not rendered anymore
    assert "panel:delfrom" in data
    assert "input:del:range" in data


@pytest.mark.asyncio
async def test_quick_still_requires_confirmation_and_all_is_gone(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 31)])

    _title, _body, quick = await delete_ui._del_panel_handler(FakeEvent(), "q:25")
    assert client.deleted == []
    assert "panel:del:qexec:25" in button_data(quick)

    _title, body, all_view = await delete_ui._del_panel_handler(FakeEvent(), "all")
    assert client.deleted == []
    assert "panel:del:allexec" not in button_data(all_view)
    assert "Preview:" not in body
    assert "temporarily unavailable" in body


@pytest.mark.asyncio
async def test_ai_and_manual_share_one_count_bound():
    from backend.ai import actions

    assert actions._MAX_DELETE_COUNT == delete_service.MAX_DELETE_COUNT
    assert DeleteTool(
        ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    ).parameters["count"]["maximum"] == delete_service.MAX_DELETE_COUNT
