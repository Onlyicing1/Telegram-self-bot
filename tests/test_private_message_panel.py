"""Private Message Management — panel surface + incoming-message enforcement.

Drives the REAL Glass UI registrations (``register_panel`` / ``register_action``
/ ``register_input``), the REAL action and input handlers, and the REAL
enforcement hook that ``backend/bot/handlers/task_events.py`` calls before it
processes anything.

The database is a fake ``bot_settings`` table, the Telegram boundary is a fake
self client, and no live Telegram, helper bot or AI is involved. The panel is
never asserted against local frontend state: every assertion reads the state the
service actually persisted (or the Telegram call the handler actually made).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from backend.bot.handlers import private_message as pm
from backend.bot.handlers import task_events
from backend.helper import inline_engine
from backend.helper import symbols as sym
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panels import get_action, get_input
from backend.services import ghost_seen_v2, private_message_service as svc

OWNER = 7770001
STRANGER = 5001
FRIEND = 5002


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeDB:
    """The three tables this feature touches: the legacy ``bot_settings``
    document (read once), the dedicated ``private_message_config`` row and the
    ``private_message_senders`` counters."""

    def __init__(self, legacy: dict[str, str] | None = None,
                 config: dict[int, dict] | None = None,
                 senders: list[dict] | None = None,
                 fail_config_write: bool = False,
                 fail_counter_read: bool = False):
        self.legacy = dict(legacy or {})
        self.config = dict(config or {})
        self.senders = list(senders or [])
        self.fail_config_write = fail_config_write
        self.fail_counter_read = fail_counter_read
        self.config_writes: list[dict] = []
        self._table = None
        self._op = None
        self._payload = None
        self._filters: list[tuple[str, object]] = []
        self._limit = None
        self._order = None
        self._single = False

    # ── chainable surface ──

    def table(self, name):
        assert name in ("bot_settings", "private_message_config", "private_message_senders"), name
        self._table, self._op, self._payload = name, None, None
        self._filters, self._limit, self._order, self._single = [], None, None, False
        return self

    def select(self, columns="*"):
        self._op = "select"
        return self

    def insert(self, payload):
        if self._table == "private_message_config" and self.fail_config_write:
            raise RuntimeError("write refused")
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        if self._table == "private_message_config" and self.fail_config_write:
            raise RuntimeError("write refused")
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, count):
        self._limit = count
        return self

    def order(self, column, desc=False):
        self._order = (column, bool(desc))
        return self

    def maybe_single(self):
        self._single = True
        return self

    # ── execution ──

    def execute(self):
        if self._table == "bot_settings":
            return self._bot_settings()
        if self._table == "private_message_config":
            return self._config()
        return self._senders_table()

    def _bot_settings(self):
        key = self._filters[0][1] if self._filters else None
        if self._op == "insert":
            self.legacy[self._payload["key"]] = self._payload["value"]
            return _FakeResult([self._payload])
        if self._op == "update":
            self.legacy[key] = self._payload["value"]
            return _FakeResult([])
        value = self.legacy.get(key)
        if self._single:
            return _FakeResult({"value": value} if value is not None else {})
        return _FakeResult([{"value": value}] if value is not None else [])

    def _owner(self) -> int | None:
        for column, value in self._filters:
            if column == "owner_id":
                return int(value)
        return None

    def _config(self):
        owner = self._owner()
        row = self.config.get(owner)
        if self._op == "insert":
            self.config[int(self._payload["owner_id"])] = {
                key: value for key, value in self._payload.items() if key != "owner_id"
            }
            self.config_writes.append(dict(self._payload))
            return _FakeResult([])
        if self._op == "update":
            if row is None:
                return _FakeResult([])
            row.update(self._payload)
            self.config_writes.append({**self._payload, "owner_id": owner})
            return _FakeResult([dict(row)])
        if row is None:
            return _FakeResult([])
        return _FakeResult([dict(row)])

    def _senders_table(self):
        if self._op == "select" and self.fail_counter_read:
            raise RuntimeError("counter store unavailable")
        if self._op == "insert":
            self.senders.append(dict(self._payload))
            return _FakeResult([dict(self._payload)])
        if self._op == "delete":
            owner = self._owner()
            self.senders = [row for row in self.senders if row.get("owner_id") != owner]
            return _FakeResult([])
        wanted = dict(self._filters)
        matched = [
            row for row in self.senders
            if all(row.get(key) == value for key, value in wanted.items())
        ]
        if self._op == "update":
            # PostgREST returns only the rows its filters actually matched, which
            # is exactly the CAS contract the claim/release pair relies on.
            for row in matched:
                row.update(self._payload)
            return _FakeResult([dict(row) for row in matched])
        if self._order:
            column, desc = self._order
            matched = sorted(matched, key=lambda row: str(row.get(column)), reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return _FakeResult([dict(row) for row in matched])


class _FakeSelfClient:
    """The Telegram boundary: handler registration, delete, send, resolution."""

    def __init__(self, dialogs=(), entities=None, delete_fails: bool = False):
        self.handlers: list = []
        self.deleted: list[tuple[int, list[int], bool]] = []
        self.sent: list[tuple[int, str]] = []
        self.dialogs = list(dialogs)
        self.entities = dict(entities or {})
        self.delete_fails = delete_fails
        self.dialogs_enumerated = 0

    def on(self, event_type):
        def _decorator(fn):
            self.handlers.append(fn)
            return fn

        return _decorator

    async def delete_messages(self, chat_id, ids, revoke=False):
        if self.delete_fails:
            raise RuntimeError("delete refused")
        self.deleted.append((chat_id, list(ids), bool(revoke)))

    async def send_message(self, entity, text):
        self.sent.append((entity, text))

    async def iter_dialogs(self):
        self.dialogs_enumerated += 1
        for dialog in self.dialogs:
            yield dialog

    async def get_entity(self, reference):
        if reference not in self.entities:
            raise ValueError(f"cannot resolve {reference}")
        return self.entities[reference]


class _FakeHelperBot:
    """The helper bot's edit channel — what the owner actually SEES after input."""

    def __init__(self):
        self.edits: list[tuple[int, int, str, list]] = []

    async def edit_message(self, chat_id, msg_id, text, buttons=None):
        self.edits.append((chat_id, msg_id, text, list(buttons or [])))


class _RecordingDispatcher:
    def __init__(self):
        self.events: list[dict] = []

    async def handle_event(self, context):
        self.events.append(context)
        return 0


def _dialog(user_id: int, first: str, last: str = "", username: str = ""):
    entity = SimpleNamespace(id=user_id, first_name=first, last_name=last, username=username)
    return SimpleNamespace(
        entity=entity,
        is_user=True,
        is_group=False,
        is_channel=False,
        unread_count=0,
        message=SimpleNamespace(text="hi", date=None),
    )


def _user(user_id: int, first: str = "Ali"):
    return SimpleNamespace(id=user_id, first_name=first, last_name="", username="")


def _user_entity(user_id: int, *, bot: bool = False, deleted: bool = False):
    """A REAL Telethon ``User`` — the gate checks the entity type and its own
    ``bot`` flag, so a duck-typed stand-in would not exercise it."""
    return types.User(
        user_id, False, False, False, deleted, bot, False, False, False, False,
        False, False, False, False, False, 1,
    )


def _event(
    chat_id: int,
    sender_id: int,
    message_id: int = 7,
    out: bool = False,
    text: str = "hello",
    *,
    is_private: bool = True,
    is_group: bool = False,
    is_channel: bool = False,
    peer_user_id: int | None = None,
    chat_entity=None,
    sender_entity=None,
    chat_resolution_fails: bool = False,
    sender_resolution_fails: bool = False,
):
    """A private-chat event with REAL entities, the way the live path delivers
    it: the peer IS the sender, and both ``get_chat()`` and ``get_sender()``
    resolve to that same human ``User``."""
    resolved_chat = chat_entity if chat_entity is not None else _user_entity(chat_id)
    resolved_sender = sender_entity if sender_entity is not None else _user_entity(sender_id)

    async def get_chat():
        if chat_resolution_fails:
            raise ValueError("cannot resolve chat")
        return resolved_chat

    async def get_sender():
        if sender_resolution_fails:
            raise ValueError("cannot resolve sender")
        return resolved_sender

    return SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        out=out,
        is_private=is_private,
        is_group=is_group,
        is_channel=is_channel,
        raw_text=text,
        mentioned=False,
        get_chat=get_chat,
        get_sender=get_sender,
        message=SimpleNamespace(
            id=message_id,
            media=None,
            reply_to_msg_id=None,
            date=datetime.now(timezone.utc),
            message=text,
            peer_id=types.PeerUser(peer_user_id if peer_user_id is not None else chat_id),
        ),
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _stored_config(db: _FakeDB) -> dict:
    """The configuration row the feature actually persisted."""
    return db.config.get(OWNER, {})


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


def _panel(panel_id: str, extra: str = ""):
    handler = get_registry().get_handler(panel_id)
    assert handler is not None, f"panel '{panel_id}' is not registered"
    return _run(handler(SimpleNamespace(), extra))


def _action(action_id: str, extra: str = "", chat_id: int = 0):
    handler = get_action(action_id)
    assert handler is not None, f"action '{action_id}' is not registered"
    return _run(handler(SimpleNamespace(), extra, chat_id))


def _input(panel_id: str, input_id: str, text: str):
    config = get_input(panel_id, input_id)
    assert config is not None, f"input '{panel_id}:{input_id}' is not registered"
    return _run(config["handler"](text, 111, 22, 333, 44))


def _edited_text(helper: _FakeHelperBot) -> str:
    assert helper.edits, "the input handler never rendered its panel back"
    return helper.edits[-1][2]


@pytest.fixture(autouse=True)
def _clean():
    svc.reset_in_memory()
    svc.configure_owner(OWNER)
    pm._rule_drafts.clear()
    task_events.configure(None)
    task_events.configure_answer_resolver(None)
    ghost_seen_v2.invalidate_manage_directory()
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_self_client(None)
    yield
    svc.reset_in_memory()
    pm._rule_drafts.clear()
    task_events.configure(None)
    task_events.configure_answer_resolver(None)
    ghost_seen_v2.invalidate_manage_directory()
    inline_engine.set_self_client(None)


@pytest.fixture()
def helper(monkeypatch):
    """A recording helper bot, so an input handler's panel edit is observable."""
    bot = _FakeHelperBot()
    monkeypatch.setattr(pm, "get_client", lambda: bot)
    return bot


@pytest.fixture()
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    return fake


@pytest.fixture()
def registered(db):
    pm.register(client=None, owner_id=OWNER, tz_str="Asia/Tehran")
    return pm


# ── registration ─────────────────────────────────────────────────────────────


def test_every_panel_action_and_input_is_registered(registered):
    for panel_id in ("pm", "pm_policy", "pm_allow", "pm_except", "pm_temp", "pm_sched", "pm_rule"):
        assert get_registry().get_handler(panel_id) is not None
    for action_id in (
        "pm_mode",
        "pm_toggle_enabled",
        "pm_refresh",
        "pm_action",
        "pm_unlimited",
        "pm_reset_window",
        "pm_reset_counters",
        "pm_page",
        "pm_allow_toggle",
        "pm_except_toggle",
        "pm_temp_set",
        "pm_temp_clear",
        "pm_sched_toggle",
        "pm_sched_new",
        "pm_sched_edit",
        "pm_sched_del",
        "pm_rule_day",
        "pm_rule_days_clear",
        "pm_rule_mode",
        "pm_rule_save",
    ):
        assert get_action(action_id) is not None, action_id
    assert get_input("pm_policy", "fixed_text") is not None
    assert get_input("pm_policy", "max_replies") is not None
    assert get_input("pm_policy", "interval") is not None
    assert get_input("pm_policy", "reset_interval") is not None
    assert get_input("pm_allow", "add") is not None
    assert get_input("pm_except", "add") is not None
    assert get_input("pm_rule", "start") is not None
    assert get_input("pm_rule", "end") is not None
    assert get_input("pm_sched", "timezone") is not None
    assert get_registry().get_parent("pm") == "menu"


def test_the_mother_menu_opens_the_private_message_panel(registered):
    from backend.bot.handlers.misc import _build_menu_buttons

    assert "panel:pm" in _datas(_build_menu_buttons())


def test_all_new_callback_data_fits_telegram_limits(registered, db):
    _panel("pm")
    _action("pm_mode", "whitelist_only")
    _action("pm_temp_set", "whitelist_only:480")
    _action("pm_sched_edit", "0")
    for panel_id in ("pm", "pm_policy", "pm_allow", "pm_except", "pm_temp", "pm_sched", "pm_rule"):
        _panel(panel_id)
    for panel_id in ("pm", "pm_policy", "pm_allow", "pm_except", "pm_temp", "pm_sched", "pm_rule"):
        _title, _body, buttons = _panel(panel_id)
        for data in _datas(buttons):
            assert len(data.encode("utf-8")) <= 64, (panel_id, data)


# ── main panel ───────────────────────────────────────────────────────────────


def test_the_main_panel_reports_the_effective_state(registered, db):
    _action("pm_mode", "whitelist_only")
    _action("pm_action", svc.ACTION_DELETE)
    _run(svc.add_list_entry("allowlist", FRIEND))
    title, body, buttons = _panel("pm")
    assert title == "Private Messages"
    assert svc.MODE_LABELS[svc.MODE_WHITELIST_ONLY] in body
    assert svc.SOURCE_LABELS[svc.SOURCE_MANUAL] in body
    assert "Enforcement:** ON" in body
    assert "Closing actions:**" in body
    assert "AI replies per person:**" in body
    assert "AI counters:**" in body
    assert "Allowlist:** 1 user(s)" in body
    assert "direct private chats** only" in body
    assert "Storage:** database" in body
    assert "does not change your Telegram privacy settings" in body
    texts = _texts(buttons)
    assert any(text.startswith(sym.STEP_COMPLETED) and "Whitelist only" in text for text in texts)
    assert any(text.startswith(sym.BULLET) and "Open" in text for text in texts)
    datas = _datas(buttons)
    assert "action:pm_mode:closed" in datas
    assert "panel:pm_policy" in datas and "panel:pm_allow" in datas
    assert "panel:pm_except" in datas and "panel:pm_temp" in datas and "panel:pm_sched" in datas


def test_the_panel_shows_the_temporary_override_with_its_remaining_time(registered, db):
    _action("pm_temp_set", "closed:30")
    _title, body, _buttons = _panel("pm")
    assert svc.MODE_LABELS[svc.MODE_CLOSED] in body
    assert svc.SOURCE_LABELS[svc.SOURCE_TEMPORARY] in body
    assert "left" in body and "Next transition:**" in body
    assert "temporary override ends" in body


def test_the_panel_reports_ephemeral_storage_without_a_database(registered, monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    _title, body, _buttons = _panel("pm")
    assert "memory only (no database configured)" in body


def test_changing_the_mode_persists_and_renders_the_persisted_state(registered, db):
    title, body, _buttons = _action("pm_mode", "closed")
    assert title == "Private Messages"
    assert svc.MODE_LABELS[svc.MODE_CLOSED] in body
    assert svc.state()["mode"] == svc.MODE_CLOSED
    assert _stored_config(db)["mode"] == svc.MODE_CLOSED


def test_an_invalid_mode_changes_nothing(registered, db):
    _title, body, _buttons = _action("pm_mode", "blocked")
    assert "not valid" in body
    assert svc.state()["mode"] == svc.MODE_OPEN
    assert db.config == {}


def test_the_enforcement_switch_pauses_and_resumes(registered, db):
    _action("pm_mode", "closed")
    _title, body, _buttons = _action("pm_toggle_enabled")
    assert "OFF (no action applied)" in body
    assert svc.state()["enabled"] is False
    _title, body, _buttons = _action("pm_toggle_enabled")
    assert svc.state()["enabled"] is True
    assert "Enforcement:** ON" in body


def test_refresh_reloads_the_persisted_state(registered, db):
    # A Phase-1 document: the refresh imports it into the dedicated row and maps
    # the single legacy policy onto the independent delete action.
    db.legacy[svc.LEGACY_STATE_ROW_KEY] = json.dumps({"mode": "closed", "policy": "delete"})
    _title, body, _buttons = _action("pm_refresh")
    assert svc.MODE_LABELS[svc.MODE_CLOSED] in body
    assert svc.state()[svc.ACTION_DELETE] is True
    assert _stored_config(db)[svc.ACTION_DELETE] is True


def test_the_restarted_panel_renders_the_state_the_database_holds(registered, db):
    _action("pm_mode", "whitelist_only")
    _run(svc.add_list_entry("allowlist", FRIEND))

    svc.reset_in_memory()  # restart: nothing in memory, the row is what survives
    _run(svc.ensure_loaded_async())
    _title, body, _buttons = _panel("pm")
    assert svc.MODE_LABELS[svc.MODE_WHITELIST_ONLY] in body
    assert "Allowlist:** 1 user(s)" in body


# ── the independent closing actions ────────────────────────────────────────


def test_the_actions_panel_carries_exactly_one_control_per_setting(registered, db):
    """Every control exists once, and the reset controls are distinct: the
    AUTOMATIC reset (toggle + interval) is not the manual reset action."""
    _title, _body, buttons = _panel("pm_policy")
    datas = _datas(buttons)
    for data in (
        "input:pm_policy:max_replies",
        "input:pm_policy:interval",
        "input:pm_policy:reset_interval",
        "action:pm_unlimited",
        "action:pm_reset_window",
        "action:pm_reset_counters",
    ):
        assert datas.count(data) == 1, (data, datas.count(data))


def test_the_panel_shows_the_reset_configuration_including_when_it_is_disabled(registered, db):
    _title, body, _buttons = _panel("pm")
    assert "Automatic counter reset:** OFF" in body

    _action("pm_reset_window")
    assert svc.state()["ai_counter_reset_enabled"] is True
    assert _stored_config(db)["ai_counter_reset_enabled"] is True
    assert _stored_config(db)["ai_counter_reset_seconds"] == svc.DEFAULT_AI_COUNTER_RESET_SECONDS

    _title, body, _buttons = _panel("pm")
    assert "OFF" not in body.split("Automatic counter reset:**")[1].splitlines()[0]
    assert "1 day" in body

    _input("pm_policy", "reset_interval", "2h")
    assert svc.state()["ai_counter_reset_seconds"] == 7200
    assert _stored_config(db)["ai_counter_reset_seconds"] == 7200
    _title, body, _buttons = _panel("pm")
    assert "2 hours" in body

    _action("pm_reset_window")
    assert svc.state()["ai_counter_reset_enabled"] is False
    _title, body, _buttons = _panel("pm")
    assert "counters only change when you reset them" in body


def test_the_reset_interval_input_validates_and_states_the_window_semantics(registered, db, helper):
    _title, body, _buttons = _panel("pm_policy")
    assert "Automatic counter reset:** OFF" in body
    # The panel documents exactly when a window starts and when the allowance
    # becomes available again.
    assert "window starts at a person's FIRST counted reply" in body
    assert "the NEXT eligible message observes the expired window" in body
    assert "only resets the COUNT" in body

    _input("pm_policy", "reset_interval", "30m")
    assert svc.state()["ai_counter_reset_seconds"] == 1800
    assert _stored_config(db)["ai_counter_reset_seconds"] == 1800

    for bad in ("nope", "-5", "5s", "400d", "", "0"):
        _input("pm_policy", "reset_interval", bad)
        assert svc.state()["ai_counter_reset_seconds"] == 1800, bad
        assert "NOT saved" in _edited_text(helper), bad
    assert _stored_config(db)["ai_counter_reset_seconds"] == 1800


def test_the_actions_panel_lists_the_three_independent_actions(registered, db):
    _action("pm_action", svc.ACTION_FIXED)
    _title, body, buttons = _panel("pm_policy")
    assert "not set" in body
    assert "Fixed response text:**" in body
    datas = _datas(buttons)
    for field_name in svc.ACTION_FIELDS:
        assert f"action:pm_action:{field_name}" in datas
    assert "input:pm_policy:fixed_text" in datas
    assert "input:pm_policy:max_replies" in datas
    assert "input:pm_policy:interval" in datas
    assert "action:pm_unlimited" in datas
    assert "action:pm_reset_counters" in datas
    assert any(text.startswith(sym.STEP_COMPLETED) and "Send fixed response" in text for text in _texts(buttons))


def test_toggling_one_action_never_enables_another(registered, db):
    _action("pm_action", svc.ACTION_DELETE)
    state = svc.state()
    assert state[svc.ACTION_DELETE] is True
    assert state[svc.ACTION_FIXED] is False and state[svc.ACTION_AI] is False
    stored = _stored_config(db)
    assert stored[svc.ACTION_DELETE] is True
    assert stored[svc.ACTION_FIXED] is False and stored[svc.ACTION_AI] is False

    _action("pm_action", svc.ACTION_DELETE)  # toggles back off
    assert svc.state()[svc.ACTION_DELETE] is False


def test_an_invalid_action_changes_nothing(registered, db):
    _title, body, _buttons = _action("pm_action", "nuke")
    assert "not valid" in body
    assert db.config == {}


def test_the_fixed_response_text_input_validates_and_persists(registered, db, helper):
    _input("pm_policy", "fixed_text", "   ")
    assert svc.state()["fixed_response_text"] == ""
    assert "cannot be empty" in _edited_text(helper)

    _input("pm_policy", "fixed_text", "x" * (svc.MAX_FIXED_RESPONSE_LENGTH + 1))
    assert svc.state()["fixed_response_text"] == ""
    assert "at most" in _edited_text(helper)

    _input("pm_policy", "fixed_text", "  I am away  ")
    assert svc.state()["fixed_response_text"] == "I am away"
    assert _stored_config(db)["fixed_response_text"] == "I am away"
    assert "I am away" in _edited_text(helper)


def test_the_ai_limit_inputs_validate_and_persist(registered, db, helper):
    _input("pm_policy", "max_replies", "7")
    assert svc.state()["ai_max_replies_per_person"] == 7
    assert _stored_config(db)["ai_max_replies_per_person"] == 7

    _input("pm_policy", "max_replies", "0")
    assert svc.state()["ai_max_replies_per_person"] == 7
    assert "NOT saved" in _edited_text(helper)

    _input("pm_policy", "interval", "0")
    assert svc.state()["ai_interval_seconds"] == 0
    assert _stored_config(db)["ai_interval_seconds"] == 0

    _input("pm_policy", "interval", "nope")
    assert svc.state()["ai_interval_seconds"] == 0
    assert "NOT saved" in _edited_text(helper)


def test_the_unlimited_toggle_and_the_counter_reset(registered, db):
    _action("pm_unlimited")
    assert svc.state()["ai_unlimited_replies"] is True
    assert _stored_config(db)["ai_unlimited_replies"] is True
    _action("pm_unlimited")
    assert svc.state()["ai_unlimited_replies"] is False

    db.senders.append({
        "owner_id": OWNER, "sender_id": STRANGER, "ai_replies_used": 3,
        "last_ai_reply_at": "2026-01-01T00:00:00+00:00",
    })
    _title, body, _buttons = _action("pm_reset_counters")
    assert "Reset AI counters" in body or "AI counters:**" in body
    assert db.senders == []


# ── allowlist / exceptions ───────────────────────────────────────────────────


def test_the_allowlist_panel_lists_a_bounded_page_of_private_chats(registered, db, monkeypatch):
    client = _FakeSelfClient(dialogs=[_dialog(1000 + index, f"User {index:02d}") for index in range(12)])
    inline_engine.set_self_client(client)
    title, body, buttons = _panel("pm_allow")
    assert title == "Allowlist"
    assert "Allowlist:** 0 user(s)" in body
    assert "Tap a recent private chat to add or remove it" in body
    datas = _datas(buttons)
    assert "action:pm_allow_toggle:1000" in datas
    assert "action:pm_allow_toggle:1007" in datas
    assert "action:pm_page:pm_allow:2" in datas
    assert len([d for d in datas if d.startswith("action:pm_allow_toggle")]) == 8

    _title, _body, buttons = _panel("pm_allow", "p=2&q=")
    datas = _datas(buttons)
    assert "action:pm_allow_toggle:1008" in datas
    assert len([d for d in datas if d.startswith("action:pm_allow_toggle")]) == 4
    assert "action:pm_page:pm_allow:1" in datas


def test_the_allowlist_toggle_adds_and_removes_a_user_and_persists(registered, db):
    client = _FakeSelfClient(dialogs=[_dialog(FRIEND, "Friend")])
    inline_engine.set_self_client(client)

    title, body, _buttons = _action("pm_allow_toggle", str(FRIEND))
    assert title == "Allowlist"
    assert svc.state()["allowlist"] == [FRIEND]
    assert _stored_config(db)["allowlist"] == [FRIEND]
    assert "Friend  ON" in "\n".join(_texts(_panel("pm_allow")[2]))

    _title, _body, _buttons = _action("pm_allow_toggle", str(FRIEND))
    assert svc.state()["allowlist"] == []


def test_the_exceptions_toggle_is_independent_of_the_allowlist(registered, db):
    client = _FakeSelfClient(dialogs=[_dialog(FRIEND, "Friend"), _dialog(STRANGER, "Stranger")])
    inline_engine.set_self_client(client)
    _action("pm_except_toggle", str(STRANGER))
    assert svc.state()["exceptions"] == [STRANGER]
    assert svc.state()["allowlist"] == []
    assert "action:pm_except_toggle:5002" in _datas(_panel("pm_except", "")[2])


def test_a_toggle_with_an_unusable_reference_changes_nothing(registered, db):
    _title, body, _buttons = _action("pm_allow_toggle", "not-a-number")
    assert "not usable" in body
    _title, body, _buttons = _action("pm_except_toggle", "0")
    assert "not usable" in body
    assert svc.state()["allowlist"] == [] and svc.state()["exceptions"] == []


def test_adding_a_user_resolves_through_telegram_before_storing(registered, db, helper):
    client = _FakeSelfClient(entities={"@friend": _user(FRIEND, "Friend")})
    inline_engine.set_self_client(client)
    _input("pm_allow", "add", "  @friend ")
    assert svc.state()["allowlist"] == [FRIEND]
    assert _stored_config(db)["allowlist"] == [FRIEND]
    assert client.deleted and client.deleted[-1][0] == 111  # the owner's reply is removed
    assert f"`{FRIEND}`" in _edited_text(helper)


def test_an_unresolvable_reference_is_refused(registered, db, helper):
    inline_engine.set_self_client(_FakeSelfClient())
    _input("pm_allow", "add", "@nobody")
    assert svc.state()["allowlist"] == []
    assert db.config == {}
    assert "could not resolve" in _edited_text(helper)


def test_a_chat_reference_is_refused_and_the_owner_cannot_add_itself(registered, db, helper):
    chat = SimpleNamespace(id=FRIEND, title="A group")  # a Chat/Channel, not a User
    inline_engine.set_self_client(_FakeSelfClient(entities={"@group": chat}))
    _input("pm_except", "add", "@group")
    assert svc.state()["exceptions"] == []
    assert "not a user" in _edited_text(helper)

    inline_engine.set_self_client(_FakeSelfClient(entities={str(OWNER): _user(OWNER)}))
    _input("pm_except", "add", str(OWNER))
    assert svc.state()["exceptions"] == []
    assert "cannot add yourself" in _edited_text(helper)


def test_without_a_connected_client_the_user_is_not_stored(registered, db, helper):
    inline_engine.set_self_client(None)
    _input("pm_allow", "add", "@friend")
    assert svc.state()["allowlist"] == []
    assert db.config == {}
    assert "not connected" in _edited_text(helper)


# ── temporary mode ───────────────────────────────────────────────────────────


def test_the_temporary_panel_offers_durations_and_clears(registered, db):
    _title, body, buttons = _panel("pm_temp")
    assert "No temporary override is active" in body
    assert "action:pm_temp_set:closed:30" in _datas(buttons)
    assert "action:pm_temp_set:whitelist_only:480" in _datas(buttons)

    title, body, _buttons = _action("pm_temp_set", "closed:480")
    assert title == "Temporary Mode"
    assert "Temporary mode:" in body and "Remaining:" in body
    assert svc.state()["temporary"]["mode"] == svc.MODE_CLOSED

    _title, body, _buttons = _action("pm_temp_clear")
    assert "No temporary override is active" in body
    assert svc.state()["temporary"] is None


def test_an_invalid_temporary_request_changes_nothing(registered, db):
    _title, body, _buttons = _action("pm_temp_set", "closed:soon")
    assert "not valid" in body
    _title, body, _buttons = _action("pm_temp_set", "blocked:30")
    assert "not valid" in body
    assert svc.state()["temporary"] is None
    assert db.config == {}


# ── schedule ─────────────────────────────────────────────────────────────────


def test_the_schedule_panel_toggles_and_shows_the_precedence(registered, db):
    _title, body, buttons = _panel("pm_sched")
    assert "Schedule:** OFF" in body
    assert "Precedence:" in body and "temporary override" in body
    assert "action:pm_sched_toggle" in _datas(buttons)

    _title, body, _buttons = _action("pm_sched_toggle")
    assert "Schedule:** ON" in body
    assert svc.state()["schedule"]["enabled"] is True


def test_the_timezone_input_validates_and_persists(registered, db, helper):
    _input("pm_sched", "timezone", "Mars/Olympus")
    assert svc.state()["schedule"]["timezone"] == svc.default_timezone()
    assert "not a usable IANA timezone" in _edited_text(helper)
    _input("pm_sched", "timezone", "  Europe/Berlin ")
    assert svc.state()["schedule"]["timezone"] == "Europe/Berlin"
    assert _stored_config(db)["schedule"]["timezone"] == "Europe/Berlin"


def test_the_rule_builder_creates_a_rule_from_the_draft(registered, db, helper):
    title, body, buttons = _action("pm_sched_new")
    assert title == "Schedule Rule"
    assert "Days:** —" in body and "new rule" in body
    assert "action:pm_rule_day:0" in _datas(buttons)

    _title, body, _buttons = _action("pm_rule_day", "0")
    _title, body, _buttons = _action("pm_rule_day", "4")
    assert "Mon" in body and "Fri" in body
    _title, body, _buttons = _action("pm_rule_mode")
    assert "Mode:**" in body
    _input("pm_rule", "start", "22:00")
    _input("pm_rule", "end", "06:00")
    assert "22:00 – 06:00" in _edited_text(helper)

    title, body, _buttons = _action("pm_rule_save")
    assert title == "Schedule"
    rule = svc.state()["schedule"]["rules"][0]
    assert rule["days"] == [0, 4] and rule["start"] == "22:00" and rule["end"] == "06:00"
    assert rule["mode"] == svc.MODE_CLOSED
    assert _stored_config(db)["schedule"]["rules"][0]["days"] == [0, 4]
    assert "1. " in body


def test_the_rule_builder_refuses_an_incomplete_rule(registered, db, helper):
    _action("pm_sched_new")
    title, body, _buttons = _action("pm_rule_save")
    assert title == "Schedule Rule"
    assert "at least one day" in body
    assert svc.state()["schedule"]["rules"] == []
    assert db.config == {}


def test_an_invalid_time_is_not_accepted_into_the_draft(registered, db, helper):
    _action("pm_sched_new")
    _input("pm_rule", "start", "25:99")
    assert "HH:MM" in _edited_text(helper)
    assert pm._rule_drafts[OWNER]["start"] == "09:00"
    _input("pm_rule", "start", "0730")
    assert "HH:MM" in _edited_text(helper)
    assert pm._rule_drafts[OWNER]["start"] == "09:00"
    _input("pm_rule", "start", "07:30")
    assert pm._rule_drafts[OWNER]["start"] == "07:30"


def test_a_rule_can_be_edited_and_deleted(registered, db, helper):
    _run(svc.save_schedule_rule({"days": [0], "start": "09:00", "end": "18:00", "mode": "closed"}))
    _run(svc.save_schedule_rule({"days": [1], "start": "10:00", "end": "11:00", "mode": "whitelist_only"}))

    title, body, buttons = _panel("pm_sched")
    assert "Mon 09:00–18:00" in body and "Tue 10:00–11:00" in body
    assert "action:pm_sched_edit:1" in _datas(buttons) and "action:pm_sched_del:1" in _datas(buttons)

    title, body, _buttons = _action("pm_sched_edit", "1")
    assert "rule 2" in body  # the draft is bound to the edited rule
    _input("pm_rule", "end", "12:30")
    _title, _body, _buttons = _action("pm_rule_save")
    assert svc.state()["schedule"]["rules"][1]["end"] == "12:30"
    assert len(svc.state()["schedule"]["rules"]) == 2

    title, body, _buttons = _action("pm_sched_del", "0")
    assert len(svc.state()["schedule"]["rules"]) == 1
    assert svc.state()["schedule"]["rules"][0]["start"] == "10:00"
    _title, body, _buttons = _action("pm_sched_del", "9")
    assert "Could not delete" in body


def test_the_schedule_panel_names_the_next_transition(registered, db, helper):
    _run(svc.add_list_entry("exceptions", FRIEND))
    _action("pm_mode", "closed")
    client = _FakeSelfClient()
    inline_engine.set_self_client(client)
    _input("pm_sched", "timezone", "UTC")
    _run(svc.save_schedule_rule({"days": list(range(7)), "start": "22:00", "end": "23:00", "mode": "open"}))
    _action("pm_sched_toggle")
    _title, body, _buttons = _panel("pm_sched")
    assert "**Next transition:**" in body
    # The rule window is wall-clock based: outside 22:00–23:00 the next boundary
    # is the rule's start, inside it the boundary that ends it. Both name the
    # rule that causes the transition, which is what the panel promises.
    assert "rule 1 start" in body or "rule 1 end" in body


# ── enforcement through the existing update path ─────────────────────────────


def _register_event_handler(client, dispatcher=None):
    task_events.register(client, OWNER, "Asia/Tehran")
    if dispatcher is not None:
        task_events.configure(dispatcher)
    assert len(client.handlers) == 1
    return client.handlers[0]


def test_an_unauthorized_message_is_deleted_and_never_dispatched(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))

    _run(handler(_event(STRANGER, STRANGER, message_id=42)))

    assert client.deleted == [(STRANGER, [42], True)]
    assert dispatcher.events == []


def test_an_unauthorized_message_with_every_action_off_touches_nothing(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_WHITELIST_ONLY))

    _run(handler(_event(STRANGER, STRANGER)))

    assert client.deleted == [] and client.sent == []
    assert dispatcher.events == []


def test_the_fixed_response_is_sent_independently_of_the_delete_and_the_ai(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_FIXED, True))
    _run(svc.set_fixed_response_text("I am away — I will read this later."))

    _run(handler(_event(STRANGER, STRANGER, message_id=1)))
    _run(handler(_event(STRANGER, STRANGER, message_id=2)))

    # The fixed response is its own action: it is sent for every eligible
    # message, and the disabled delete/AI actions do nothing.
    assert client.sent == [
        (STRANGER, "I am away — I will read this later."),
        (STRANGER, "I am away — I will read this later."),
    ]
    assert client.deleted == []
    assert db.senders == []
    assert dispatcher.events == []


def test_delete_and_fixed_response_apply_in_order_on_the_same_message(registered, db):
    client = _FakeSelfClient()
    handler = _register_event_handler(client, _RecordingDispatcher())
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))
    _run(svc.set_action(svc.ACTION_FIXED, True))
    _run(svc.set_fixed_response_text("Closed for now."))

    _run(handler(_event(STRANGER, STRANGER, message_id=5)))

    assert client.deleted == [(STRANGER, [5], True)]
    assert client.sent == [(STRANGER, "Closed for now.")]


def test_an_authorized_sender_still_reaches_the_existing_handlers(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))
    _run(svc.add_list_entry("exceptions", FRIEND))

    _run(handler(_event(FRIEND, FRIEND, message_id=5)))

    assert len(dispatcher.events) == 1
    assert dispatcher.events[0]["message_id"] == 5
    assert client.deleted == []


def test_open_mode_and_paused_enforcement_and_the_owners_own_messages_are_untouched(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)

    _run(handler(_event(STRANGER, STRANGER)))  # default: open
    assert len(dispatcher.events) == 1

    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))
    _run(handler(_event(STRANGER, STRANGER, message_id=2)))
    assert len(dispatcher.events) == 1  # restricted

    _run(svc.set_enabled(False))
    _run(handler(_event(STRANGER, STRANGER, message_id=3)))
    assert len(dispatcher.events) == 2  # paused: nothing is enforced

    _run(svc.set_enabled(True))
    _run(handler(_event(OWNER, OWNER, message_id=4, out=True)))
    assert len(dispatcher.events) == 3  # the owner is never restricted
    # Only the ONE message that was restricted while enforcement was ON acted.
    assert client.deleted == [(STRANGER, [2], True)]


def test_a_failed_delete_still_stops_the_message_without_raising(registered, db):
    client = _FakeSelfClient(delete_fails=True)
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))

    _run(handler(_event(STRANGER, STRANGER, message_id=9)))  # must not raise

    assert dispatcher.events == []


def test_enforcement_applies_without_any_task_dispatcher_or_resolver(registered, db):
    client = _FakeSelfClient()
    task_events.register(client, OWNER, "Asia/Tehran")
    handler = client.handlers[0]
    assert task_events.get_dispatcher() is None and task_events.get_answer_resolver() is None
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))

    _run(handler(_event(STRANGER, STRANGER, message_id=11)))

    assert client.deleted == [(STRANGER, [11], True)]


def test_group_messages_are_outside_this_feature(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))

    event = _event(-1001234567890, STRANGER, message_id=13, is_private=False, is_group=True)
    _run(handler(event))

    assert client.deleted == []
    assert len(dispatcher.events) == 1


def test_a_bot_in_a_private_chat_is_never_touched(registered, db):
    """A private chat WITH A BOT is still a private chat — and must be excluded."""
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))
    _run(svc.set_action(svc.ACTION_FIXED, True))
    _run(svc.set_action(svc.ACTION_AI, True))
    _run(svc.set_fixed_response_text("Closed."))
    bot = _user_entity(STRANGER, bot=True)

    _run(handler(_event(STRANGER, STRANGER, message_id=21, chat_entity=bot, sender_entity=bot)))

    assert client.deleted == [] and client.sent == []
    assert db.senders == []
    assert len(dispatcher.events) == 1


def test_an_unresolvable_sender_is_never_touched(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))

    _run(handler(_event(STRANGER, STRANGER, message_id=23, sender_resolution_fails=True)))

    assert client.deleted == []
    assert len(dispatcher.events) == 1


def test_the_ai_response_is_sent_with_the_two_required_disclosures(registered, db, monkeypatch):
    """The AI action goes through the existing AI runtime and its own allowance."""
    import backend.ai.engine.engine as engine_module

    calls: list[dict] = []

    class _Response:
        success = True
        text = "لطفاً بعداً پیام بدهید."

    class _Providers:
        async def chat(self, messages, **kwargs):
            calls.append({"messages": messages, "kwargs": kwargs})
            return _Response()

    class _Engine:
        provider_manager = _Providers()

    async def _apply(owner_id):
        return True

    monkeypatch.setattr(engine_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(engine_module, "apply_persisted_config", _apply)

    client = _FakeSelfClient()
    handler = _register_event_handler(client, _RecordingDispatcher())
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_AI, True))
    _run(svc.set_ai_max_replies_per_person(2))
    _run(svc.set_ai_interval_seconds(0))

    _run(handler(_event(STRANGER, STRANGER, message_id=31, text="سلام")))

    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"tools": []}
    assert [message["role"] for message in calls[0]["messages"]] == ["system", "user"]
    assert calls[0]["messages"][1]["content"] == "سلام"
    assert len(client.sent) == 1
    sent_text = client.sent[0][1]
    assert sent_text.endswith(f"{svc.CLOSED_NOTICE}\n{svc.AI_NOTICE}")
    assert "لطفاً بعداً پیام بدهید." in sent_text
    assert db.senders[0]["ai_replies_used"] == 1


def test_the_decision_seen_by_the_handler_is_the_persisted_one(registered, db):
    """The handler enforces what the DATABASE holds after a restart, not a default."""
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_action(svc.ACTION_DELETE, True))
    svc.reset_in_memory()
    _run(svc.ensure_loaded_async())

    client = _FakeSelfClient()
    handler = _register_event_handler(client, _RecordingDispatcher())
    _run(handler(_event(STRANGER, STRANGER, message_id=17)))

    assert client.deleted == [(STRANGER, [17], True)]
