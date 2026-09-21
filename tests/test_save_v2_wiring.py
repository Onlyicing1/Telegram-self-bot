"""SAVE V2 part 2 — manual Save + AI Save metadata wiring.

Phase under test: the two USER-FACING save surfaces feeding the Part 1 data
contract. The Glass Save panel gains one optional ``Name & tags`` step (its own
row, never prompted for), and the AI Save tools accept the same two optional
fields (``display_name``/``tags``) end to end: JSON action → validation →
``resolve_tool_calls`` → tool arguments → the tool's ``SaveMetadata`` →
``execute_save`` → ``saved_items``.

Nothing later in Save V2 is implemented or claimed here: no search, no semantic
retrieval, no candidate/ambiguity handling, no post-save rename/tag editing, no
management UI. The tests below therefore prove the two WIRING paths and the
shared rules they must both obey — including the ones that must NOT happen: an
explicitly declined tag list stays empty even when the model proposes tags, an
unhonourable name/tag refuses before any transfer, and ``save_code`` stays
application-owned.

The manual end-to-end tests reproduce the input listener's real contract —
``clear_pending`` FIRST, then call the pending handler — because Reply Mode's
armed metadata travels inside the pending entry (a handler closure). Code that
read the popped state back inside the handler would pass a naive test and fail
in production, so the tests pop the way the listener does.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from telethon.tl.types import DocumentAttributeFilename, MessageMediaDocument

from backend.ai.actions import (
    ALLOWED_FIELDS,
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    KIND_INVALID,
    explicit_no_tags_requested,
    parse_action_text,
    parse_command_intent,
    save_metadata_requested,
    validate_action,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.save import SaveByLinkTool, SaveTool
from backend.db import client as db_client
from backend.helper import input_state
from backend.helper.panels import get_input
from backend.services import save_service
from backend.services.save_service import SaveMetadata

from tests.test_12_save_engine import (
    FakeDoc,
    FakeMessage,
    MockClient,
    _save_code,
)

OWNER = 42
CHAT_ID = 111
REPLY_MSG_ID = 444
TARGET_MSG_ID = 555

NAME = "University Weekly Schedule — Semester Two"


@pytest.fixture(autouse=True)
def reset_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    input_state.clear_all()
    yield
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    input_state.clear_all()


# ─── fixtures / fakes ────────────────────────────────────────────────────────


def _doc_message() -> FakeMessage:
    doc = FakeDoc()
    doc.mime_type = "application/pdf"
    doc.size = 4321
    doc.attributes = [DocumentAttributeFilename("schedule.pdf")]
    return FakeMessage(media=MessageMediaDocument(document=doc, ttl_seconds=None))


class _Reply(FakeMessage):
    """The owner's own message in Reply Mode — a reply to the target."""

    reply_to_msg_id = TARGET_MSG_ID


class WiringClient(MockClient):
    """MockClient + the reply/link lookups the save paths perform."""

    def __init__(self, target=None):
        super().__init__()
        self.target = target if target is not None else _doc_message()
        self.reply = _Reply(chat_id=CHAT_ID, msg_id=REPLY_MSG_ID)
        self.get_messages_calls: list = []

    async def get_messages(self, chat_id, ids=None, **kwargs):
        self.get_messages_calls.append((chat_id, ids))
        return self.reply if ids == REPLY_MSG_ID else self.target

    async def delete_messages(self, chat_id, ids):
        self.calls.append(("delete_messages", chat_id, ids))


class _Telegram:
    """The TelegramAPI facade the tools reach the client through."""

    def __init__(self, client):
        self.client = client


def _ai_context(client, request_text: str) -> ToolContext:
    return ToolContext(
        telegram=_Telegram(client),
        owner_id=OWNER,
        tz_str="UTC",
        extra={
            "chat_id": CHAT_ID,
            # The dispatcher carries the REPLIED-TO message the save targets.
            "reply_msg": {"chat_id": CHAT_ID, "message_id": TARGET_MSG_ID},
            "request_text": request_text,
        },
    )


async def _saved_row(confirmation: str) -> dict:
    row = await db_client.query_save(_save_code(confirmation))
    assert row is not None, f"no stored row for {confirmation!r}"
    return row


# ═══ AI SAVE — the complete parameter path ═══════════════════════════════════

# ── 1. the tool schema the model sees ──


def test_save_tool_exposes_the_owner_metadata_as_optional_parameters():
    params = SaveTool.parameters.fget(SaveTool)
    assert sorted(params) == ["display_name", "tags"]
    assert params["display_name"]["type"] == "string"
    assert params["tags"]["type"] == "array"
    # Optional, never required: a metadata-less save is the default.
    assert all("required" not in field for field in params.values())


def test_save_by_link_tool_exposes_the_same_optional_metadata():
    params = SaveByLinkTool.parameters.fget(SaveByLinkTool)
    assert sorted(params) == ["display_name", "link", "tags"]
    # The link stays the only REQUIRED argument; the metadata is optional.
    assert SaveByLinkTool.required_arguments.fget(SaveByLinkTool) == ("link",)


def test_allowed_fields_permits_the_metadata_fields():
    assert {"display_name", "tags"} <= ALLOWED_FIELDS


# ── 2. validation + resolution ──


def test_metadata_survives_validation_and_resolution():
    r = parse_action_text(
        '{"action":"save","target":"replied_message",'
        f'"display_name":"{NAME}","tags":["university","semester-2"]}}'
    )
    assert r.kind == KIND_EXECUTABLE
    assert r.display_name == NAME
    assert r.tags == ["university", "semester-2"]
    assert r.tool_calls == [
        {
            "name": "save",
            "arguments": {"display_name": NAME, "tags": ["university", "semester-2"]},
        }
    ]


def test_deep_save_carries_the_metadata_too():
    r = parse_action_text(
        '{"action":"deep_save","target":"replied_message","tags":["university"]}'
    )
    assert r.tool_calls == [{"name": "save", "arguments": {"tags": ["university"]}}]


def test_save_link_carries_the_metadata_with_the_verbatim_url():
    url = "https://t.me/SomeChannel/42"
    r = parse_action_text(
        f'{{"action":"save_link","link":"{url}","display_name":"Slides","tags":["semester-2"]}}'
    )
    assert r.tool_calls == [
        {
            "name": "save_by_link",
            "arguments": {
                "link": url,
                "display_name": "Slides",
                "tags": ["semester-2"],
            },
        }
    ]


def test_metadata_less_save_keeps_the_existing_tool_call_shape():
    for payload in ('{"action":"save","target":"replied_message"}',
                    '{"action":"deep_save","target":"replied_message"}'):
        assert parse_action_text(payload).tool_calls == [
            {"name": "save", "arguments": {}}
        ]


def test_explicit_no_tags_arrives_as_an_empty_list():
    r = parse_action_text('{"action":"save","target":"replied_message","tags":[]}')
    assert r.tags == []
    assert r.tool_calls == [{"name": "save", "arguments": {"tags": []}}]


def test_an_empty_display_name_is_not_a_name():
    r = parse_action_text('{"action":"save","target":"replied_message","display_name":""}')
    # "" means "the owner named nothing" — it is not carried as a name at all.
    assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_metadata_is_rejected_on_unrelated_actions():
    # Each payload is otherwise perfectly valid — only the metadata makes it
    # invalid, so a metadata field can never ride along with another action.
    payloads = (
        '{"action":"delete_messages","target":"last_message","count":1,"tags":["x"]}',
        '{"action":"list_recent_messages","count":1,"tags":["x"]}',
        '{"action":"database_stats","display_name":"X"}',
    )
    for payload in payloads:
        r = parse_action_text(payload)
        assert r.kind == KIND_INVALID, payload
        assert "display_name" in r.error or "tags" in r.error


def test_metadata_types_are_validated():
    assert parse_action_text(
        '{"action":"save","target":"replied_message","display_name":5}'
    ).kind == KIND_INVALID
    assert parse_action_text(
        '{"action":"save","target":"replied_message","tags":"university"}'
    ).kind == KIND_INVALID
    assert parse_action_text(
        '{"action":"save","target":"replied_message","tags":[1,2]}'
    ).kind == KIND_INVALID


def test_length_rules_stay_with_the_shared_service_not_the_validator():
    # The action layer checks SHAPE only; an over-long name is the service's
    # honest refusal (tested at the tool level below), so the two rule sets can
    # never drift apart.
    long_name = "x" * (save_service.MAX_DISPLAY_NAME_CHARS + 1)
    r = parse_action_text(
        f'{{"action":"save","target":"replied_message","display_name":"{long_name}"}}'
    )
    assert r.kind == KIND_EXECUTABLE


def test_save_code_is_still_not_model_controlled_by_the_metadata_path():
    # A save action may not carry a save_code at all — the code is generated by
    # the application, never chosen by the model.
    r = parse_action_text(
        '{"action":"save","target":"replied_message","save_code":"HACK1"}'
    )
    assert r.kind == KIND_INVALID
    assert r.tool_calls == []


# ── 3. the deterministic fast path must not drop metadata ──


def test_the_fast_path_defers_when_a_name_is_asked_for():
    r = parse_command_intent("اینو به اسم برنامه دانشگاه سیو کن", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL
    r_en = parse_command_intent("save this as University Schedule", has_reply=True)
    assert r_en.kind == KIND_CONVERSATIONAL


def test_the_fast_path_defers_when_tags_are_asked_for():
    for text in ("اینو با تگ دانشگاه سیو کن", "save this and tag it university"):
        assert parse_command_intent(text, has_reply=True).kind == KIND_CONVERSATIONAL


def test_the_fast_path_defers_when_the_owner_declines_tags():
    r = parse_command_intent("save this without tags", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL


def test_the_fast_path_still_resolves_a_plain_save():
    for text in ("اینو سیو کن", "save this"):
        r = parse_command_intent(text, has_reply=True)
        assert r.kind == KIND_EXECUTABLE
        assert r.tool_calls == [{"name": "save", "arguments": {}}]


def test_as_idioms_are_not_names():
    assert not save_metadata_requested("save this as well")
    assert not save_metadata_requested("save this as usual")
    assert save_metadata_requested("save this as University Schedule")


def test_no_tags_vocabulary():
    for text in ("save this without tags", "save this, no tags", "dont tag this",
                 "بدون تگ سیو کن", "تگ نزن"):
        assert explicit_no_tags_requested(text), text
    assert not explicit_no_tags_requested("save this with tags university")


# ── 4. execution: arguments → SaveMetadata → execute_save → saved_items ──


@pytest.mark.asyncio
async def test_save_tool_persists_the_model_proposed_metadata():
    client = WiringClient()
    ctx = _ai_context(client, "save this as University Schedule and tag it university")
    tool = SaveTool(ctx)

    result = await tool.execute(
        ctx, {"display_name": NAME, "tags": ["university", "semester-2"]}
    )

    assert result.success is True
    row = await _saved_row(result.message)
    assert row["display_name"] == NAME
    assert row["tags"] == ["university", "semester-2"]
    assert row["owner_id"] == OWNER
    assert re.fullmatch(r"S\w{4}", row["save_code"])
    # The replied-to target was resolved through the SAME client the runtime
    # injected (never a second client, never a fake message).
    assert (CHAT_ID, TARGET_MSG_ID) in client.get_messages_calls


@pytest.mark.asyncio
async def test_save_tool_hands_execute_save_the_shared_contract():
    client = WiringClient()
    ctx = _ai_context(client, "save this as University Schedule")
    captured: dict = {}

    async def fake_execute_save(client_, owner_id, reply_msg, tz_str, *, metadata=None):
        captured["metadata"] = metadata
        return "✅ Saved S9999"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(save_service, "execute_save", fake_execute_save)
        await SaveTool(ctx).execute(ctx, {"display_name": NAME})

    assert isinstance(captured["metadata"], SaveMetadata)
    assert captured["metadata"].display_name == NAME
    assert captured["metadata"].tags == ()


@pytest.mark.asyncio
async def test_save_tool_without_metadata_is_unchanged():
    client = WiringClient()
    ctx = _ai_context(client, "اینو سیو کن")

    result = await SaveTool(ctx).execute(ctx, {})

    assert result.success is True
    row = await _saved_row(result.message)
    assert row.get("display_name") is None
    assert row["tags"] == []  # no invented hashtags, no invented name


@pytest.mark.asyncio
async def test_the_owner_declining_tags_beats_a_model_proposal():
    client = WiringClient()
    ctx = _ai_context(client, "save this without tags")
    # The model proposes tags anyway; the owner's own words are authoritative.
    result = await SaveTool(ctx).execute(ctx, {"tags": ["invented", "extra"]})

    assert result.success is True
    row = await _saved_row(result.message)
    assert row["tags"] == []
    assert row.get("display_name") is None


@pytest.mark.asyncio
async def test_an_unhonourable_name_refuses_before_any_transfer():
    client = WiringClient()
    ctx = _ai_context(client, "save this")
    too_long = "x" * (save_service.MAX_DISPLAY_NAME_CHARS + 1)

    result = await SaveTool(ctx).execute(ctx, {"display_name": too_long})

    assert result.success is False
    assert "Nothing was saved" in result.message
    assert client.calls == []                    # no download, no upload
    assert client.get_messages_calls == []       # not even the reply fetch
    assert db_client._fallback["saved_items"] == []


@pytest.mark.asyncio
async def test_too_many_tags_refuse_before_any_transfer():
    client = WiringClient()
    ctx = _ai_context(client, "save this")
    tags = [f"tag-{i}" for i in range(save_service.MAX_SAVE_TAGS + 1)]

    result = await SaveTool(ctx).execute(ctx, {"tags": tags})

    assert result.success is False
    assert client.calls == []
    assert db_client._fallback["saved_items"] == []


@pytest.mark.asyncio
async def test_save_code_stays_application_owned():
    client = WiringClient()
    ctx = _ai_context(client, "save this")
    result = await SaveTool(ctx).execute(
        ctx, {"save_code": "HACK1", "display_name": "Named item"}
    )

    row = await _saved_row(result.message)
    assert row["save_code"] != "HACK1"
    assert row["display_name"] == "Named item"


@pytest.mark.asyncio
async def test_save_by_link_tool_persists_the_metadata_end_to_end():
    client = WiringClient()
    ctx = _ai_context(client, "save this link")
    tool = SaveByLinkTool(ctx)

    result = await tool.execute(
        ctx,
        {
            "link": "https://t.me/c/3080318802/42",
            "display_name": NAME,
            "tags": ["university"],
        },
    )

    assert result.success is True
    row = await _saved_row(result.message)
    assert row["display_name"] == NAME
    assert row["tags"] == ["university"]


@pytest.mark.asyncio
async def test_save_by_link_without_metadata_is_unchanged():
    client = WiringClient()
    ctx = _ai_context(client, "این لینک رو سیو کن")
    tool = SaveByLinkTool(ctx)

    result = await tool.execute(ctx, {"link": "https://t.me/c/3080318802/42"})

    row = await _saved_row(result.message)
    assert row.get("display_name") is None
    assert row["tags"] == []


# ═══ MANUAL SAVE — the Glass panel path ══════════════════════════════════════

# ── 5. the optional step exists and documents itself ──


def _button_datas(buttons) -> list[str]:
    datas = []
    for row in buttons:
        for btn in row:
            data = getattr(btn, "data", None)
            if isinstance(data, bytes):
                datas.append(data.decode("utf-8", errors="replace"))
            elif isinstance(data, str):
                datas.append(data)
    return datas


@pytest.mark.asyncio
async def test_the_source_panel_offers_the_optional_metadata_step():
    from backend.bot.handlers import save as save_handler

    _title, body, buttons = await save_handler._save_panel_handler(None, "type:")
    datas = _button_datas(buttons)
    assert "input:save:meta" in datas
    assert "action:save_reply" in datas          # Reply Mode is still the default
    assert "optional" in body.lower()

    # The inline (helper-bot) rendering of the same step carries it too.
    result = (await save_handler._save_inline_builder(None, "type:"))[0]
    assert "Name & tags" in result.send_message.message


def test_the_metadata_input_documents_its_one_line_format():
    from backend.bot.handlers import save as save_handler

    save_handler.register(MagicMock(), OWNER, "UTC")  # inputs register at wiring time
    spec = get_input("save", "meta")
    assert spec is not None
    assert "Name | tag, tag" in spec["prompt"]
    assert "nothing is invented" in spec["prompt"]


# ── 6. deterministic parsing (no LLM, no invented tags) ──


@pytest.mark.parametrize(
    "line,expected_name,expected_tags",
    [
        (NAME, NAME, ()),
        ("University Schedule | university, semester-2", "University Schedule",
         ("university", "semester-2")),
        ("| university", None, ("university",)),
        ("University Schedule | -", "University Schedule", ()),
        ("University Schedule | none", "University Schedule", ()),
        ("University Schedule |", "University Schedule", ()),
        ("  Schedule   X  |   a   b , c ", "Schedule X", ("a b", "c")),
        ("X | Uni, uni", "X", ("Uni",)),          # case-insensitive dedupe, first spelling
        ("X | , ,", "X", ()),                     # empty parts are not tags
    ],
)
def test_metadata_line_parsing(line, expected_name, expected_tags):
    from backend.bot.handlers.save import parse_metadata_line

    metadata = parse_metadata_line(line)
    assert metadata.display_name == expected_name
    assert metadata.tags == expected_tags


@pytest.mark.parametrize(
    "line",
    ["", "   ", "A | B | C", "| -", "| none"],
)
def test_unreadable_metadata_lines_are_refused(line):
    from backend.bot.handlers.save import parse_metadata_line

    with pytest.raises(ValueError):
        parse_metadata_line(line)


def test_metadata_line_enforces_the_shared_limits_honestly():
    from backend.bot.handlers.save import parse_metadata_line

    max_ok = ", ".join(f"t{i}" for i in range(save_service.MAX_SAVE_TAGS))
    metadata = parse_metadata_line(f"N | {max_ok}")
    assert len(metadata.tags) == save_service.MAX_SAVE_TAGS

    # Over the shared bounds is REFUSED by the shared normalizer, never
    # truncated into a smaller tag list behind the owner's back.
    with pytest.raises(ValueError):
        parse_metadata_line("N | " + ", ".join(f"t{i}" for i in range(20)))
    with pytest.raises(ValueError):
        parse_metadata_line("N | " + "x" * (save_service.MAX_TAG_CHARS + 1))


# ── 7. the input step arms Reply Mode with the metadata inside the pending entry ──


class _Helper:
    def __init__(self):
        self.edits: list = []

    async def edit_message(self, chat_id, msg_id, text, buttons=None, **kwargs):
        self.edits.append({"text": text, "buttons": buttons})
        return type("M", (), {"id": msg_id})()


@pytest.mark.asyncio
async def test_the_metadata_step_arms_reply_mode_with_the_metadata(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    await save_handler._save_metadata_input_handler(
        f"{NAME} | university, semester-2", CHAT_ID, 900, CHAT_ID, 700
    )

    pending = input_state.get_pending(OWNER)
    assert pending is not None
    assert pending["panel_id"] == "save_reply"
    assert pending["chat_id"] == CHAT_ID
    assert pending["timeout"] is None          # unbounded, like Deep Save
    assert pending["inline_chat_id"] == CHAT_ID
    assert pending["inline_msg_id"] == 700
    # The metadata rides INSIDE the pending entry (a closure), because the
    # listener pops the entry before it calls the handler.
    assert pending["handler"] is not save_handler._save_reply_wait_handler

    echo = helper.edits[-1]["text"]
    assert NAME in echo and "university, semester-2" in echo
    assert ("delete_messages", CHAT_ID, [900]) in client.calls


@pytest.mark.asyncio
async def test_an_unreadable_metadata_step_keeps_the_panel_and_arms_nothing(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    await save_handler._save_metadata_input_handler(
        "A | B | C", CHAT_ID, 900, CHAT_ID, 700
    )

    assert input_state.get_pending(OWNER) is None
    notice = helper.edits[-1]["text"]
    assert "one |" in notice
    assert "input:save:meta" in _button_datas(helper.edits[-1]["buttons"] or [])


# ── 8. the full manual path: step → reply → execute_save → saved_items ──


@pytest.mark.asyncio
async def test_armed_metadata_survives_the_listener_clear_and_is_persisted(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    await save_handler._save_metadata_input_handler(
        f"{NAME} | university, semester-2", CHAT_ID, 900, CHAT_ID, 700
    )

    # Exactly what the input listener does with the owner's reply.
    entry = input_state.clear_pending(OWNER)
    assert entry is not None
    await entry["handler"]("ignored", CHAT_ID, REPLY_MSG_ID, CHAT_ID, 700)

    row = (await db_client.list_saves(OWNER))[0][0]
    assert row["display_name"] == NAME
    assert row["tags"] == ["university", "semester-2"]
    assert row["owner_id"] == OWNER
    assert re.fullmatch(r"S\w{4}", row["save_code"])
    assert row["media_type"] == "Document"
    assert row["mime_type"] == "application/pdf"
    # The reply resolved to the exact replied-to target, never to the reply.
    assert (CHAT_ID, REPLY_MSG_ID) in client.get_messages_calls
    assert not any(c[0] == "forward_messages" for c in client.calls)


@pytest.mark.asyncio
async def test_plain_reply_mode_is_unchanged_and_arms_no_metadata(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    class _Event:
        message_id = 700

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    await save_handler._save_reply_action(_Event(), "", CHAT_ID)

    pending = input_state.get_pending(OWNER)
    assert pending["handler"] is save_handler._save_reply_wait_handler

    entry = input_state.clear_pending(OWNER)
    await entry["handler"]("ignored", CHAT_ID, REPLY_MSG_ID, CHAT_ID, 700)

    row = (await db_client.list_saves(OWNER))[0][0]
    assert row.get("display_name") is None
    assert row["tags"] == []


@pytest.mark.asyncio
async def test_an_unrelated_pending_state_never_becomes_save_metadata(monkeypatch):
    """The metadata comes from the step that armed Reply Mode — nothing else.

    A pending entry left by another flow (here: a stale ``extra`` string like
    the retrieve inputs carry) must not be reinterpreted as a name or tags.
    """
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    async def _other_handler(*args, **kwargs):
        raise AssertionError("the unrelated handler must not run")

    input_state.set_pending(OWNER, "retrieve_item", _other_handler, CHAT_ID, "x",
                            extra="SAXCK")
    await save_handler._save_reply_action(type("E", (), {"message_id": 700})(), "", CHAT_ID)

    entry = input_state.clear_pending(OWNER)
    await entry["handler"]("ignored", CHAT_ID, REPLY_MSG_ID, CHAT_ID, 700)

    row = (await db_client.list_saves(OWNER))[0][0]
    assert row.get("display_name") is None
    assert row["tags"] == []


@pytest.mark.asyncio
async def test_an_invalid_metadata_line_never_reaches_the_save(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine

    helper = _Helper()
    client = WiringClient()
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    await save_handler._save_metadata_input_handler(
        "A | B | C", CHAT_ID, 900, CHAT_ID, 700
    )

    assert input_state.get_pending(OWNER) is None
    # The cleanup delete of the owner's input line is expected; what must NOT
    # happen is any transfer or row — nothing was saved.
    assert not any(c[0] in ("send_file", "send_message", "download_media")
                   for c in client.calls)
    assert db_client._fallback["saved_items"] == []
