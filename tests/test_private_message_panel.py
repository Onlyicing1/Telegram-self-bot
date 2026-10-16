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


class _FakeBotSettingsDB:
    """A generic ``bot_settings`` key/value fake (extended by every feature)."""

    def __init__(self, rows: dict[str, str] | None = None):
        self.rows = dict(rows or {})
        self.inserts: list[dict] = []
        self.updates: list[dict] = []
        self._key: str | None = None
        self._selecting_value = False
        self._single = False
        self._pending_update = None
        self._pending_insert = None

    def table(self, name):
        assert name == "bot_settings"
        return self

    def select(self, columns):
        self._selecting_value = str(columns).strip() == "value"
        self._single = False
        return self

    def eq(self, column, key):
        assert column == "key"
        self._key = key
        return self

    def maybe_single(self):
        self._single = True
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def execute(self):
        assert self._key is not None
        if self._pending_insert is not None:
            payload, self._pending_insert = self._pending_insert, None
            self.rows[payload["key"]] = payload["value"]
            self.inserts.append(payload)
            return _FakeResult([payload])
        if self._pending_update is not None:
            payload, self._pending_update = self._pending_update, None
            self.rows[self._key] = payload["value"]
            self.updates.append({"key": self._key, **payload})
            return _FakeResult([])
        value = self.rows.get(self._key)
        if self._selecting_value:
            if self._single:
                return _FakeResult({"value": value} if value is not None else {})
            return _FakeResult([{"value": value}] if value is not None else [])
        return _FakeResult([{"key": self._key}] if value is not None else [])


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


def _event(chat_id: int, sender_id: int, message_id: int = 7, out: bool = False, text: str = "hello"):
    return SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        out=out,
        is_private=True,
        raw_text=text,
        mentioned=False,
        message=SimpleNamespace(
            id=message_id, media=None, reply_to_msg_id=None, date=datetime.now(timezone.utc)
        ),
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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
    fake = _FakeBotSettingsDB()
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
        "pm_policy",
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
    assert get_input("pm_policy", "autoreply") is not None
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
    _action("pm_policy", "delete")
    _run(svc.add_list_entry("allowlist", FRIEND))
    title, body, buttons = _panel("pm")
    assert title == "Private Messages"
    assert svc.MODE_LABELS[svc.MODE_WHITELIST_ONLY] in body
    assert svc.SOURCE_LABELS[svc.SOURCE_MANUAL] in body
    assert "Enforcement:** ON" in body
    assert svc.POLICY_LABELS[svc.POLICY_DELETE] in body
    assert "Allowlist:** 1 user(s)" in body
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
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["mode"] == svc.MODE_CLOSED


def test_an_invalid_mode_changes_nothing(registered, db):
    _title, body, _buttons = _action("pm_mode", "blocked")
    assert "not valid" in body
    assert svc.state()["mode"] == svc.MODE_OPEN
    assert db.rows == {}


def test_the_enforcement_switch_pauses_and_resumes(registered, db):
    _action("pm_mode", "closed")
    _title, body, _buttons = _action("pm_toggle_enabled")
    assert "OFF (no policy action)" in body
    assert svc.state()["enabled"] is False
    _title, body, _buttons = _action("pm_toggle_enabled")
    assert svc.state()["enabled"] is True
    assert "Enforcement:** ON" in body


def test_refresh_reloads_the_persisted_state(registered, db):
    db.rows[svc.STATE_ROW_KEY] = json.dumps({"mode": "closed", "policy": "delete"})
    _title, body, _buttons = _action("pm_refresh")
    assert svc.MODE_LABELS[svc.MODE_CLOSED] in body
    assert svc.POLICY_LABELS[svc.POLICY_DELETE] in body


def test_the_restarted_panel_renders_the_state_the_database_holds(registered, db):
    _action("pm_mode", "whitelist_only")
    _run(svc.add_list_entry("allowlist", FRIEND))

    svc.reset_in_memory()  # restart: nothing in memory, the row is what survives
    _run(svc.ensure_loaded_async())
    _title, body, _buttons = _panel("pm")
    assert svc.MODE_LABELS[svc.MODE_WHITELIST_ONLY] in body
    assert "Allowlist:** 1 user(s)" in body


# ── unauthorized policy ──────────────────────────────────────────────────────


def test_the_policy_panel_lists_the_three_policies_and_the_auto_reply_text(registered, db):
    _action("pm_policy", "autoreply")
    _title, body, buttons = _panel("pm_policy")
    assert "not set" in body
    assert "Auto-reply**" in body or "Auto-reply text" in body
    assert "action:pm_policy:ignore" in _datas(buttons)
    assert "input:pm_policy:autoreply" in _datas(buttons)
    assert any(text.startswith(sym.STEP_COMPLETED) and "Auto-reply" in text for text in _texts(buttons))


def test_the_auto_reply_text_input_validates_and_persists(registered, db, helper):
    _input("pm_policy", "autoreply", "   ")
    assert svc.state()["autoreply_text"] == ""
    assert "cannot be empty" in _edited_text(helper)

    _input("pm_policy", "autoreply", "x" * (svc.MAX_AUTOREPLY_LENGTH + 1))
    assert svc.state()["autoreply_text"] == ""
    assert "at most" in _edited_text(helper)

    _input("pm_policy", "autoreply", "  I am away  ")
    assert svc.state()["autoreply_text"] == "I am away"
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["autoreply_text"] == "I am away"
    assert "I am away" in _edited_text(helper)


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
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["allowlist"] == [FRIEND]
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
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["allowlist"] == [FRIEND]
    assert client.deleted and client.deleted[-1][0] == 111  # the owner's reply is removed
    assert f"`{FRIEND}`" in _edited_text(helper)


def test_an_unresolvable_reference_is_refused(registered, db, helper):
    inline_engine.set_self_client(_FakeSelfClient())
    _input("pm_allow", "add", "@nobody")
    assert svc.state()["allowlist"] == []
    assert db.rows == {}
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
    assert db.rows == {}
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
    assert db.rows == {}


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
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["schedule"]["timezone"] == "Europe/Berlin"


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
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["schedule"]["rules"][0]["days"] == [0, 4]
    assert "1. " in body


def test_the_rule_builder_refuses_an_incomplete_rule(registered, db, helper):
    _action("pm_sched_new")
    title, body, _buttons = _action("pm_rule_save")
    assert title == "Schedule Rule"
    assert "at least one day" in body
    assert svc.state()["schedule"]["rules"] == []
    assert db.rows == {}


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
    assert "rule 1 start" in body


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
    _run(svc.set_policy(svc.POLICY_DELETE))

    _run(handler(_event(STRANGER, STRANGER, message_id=42)))

    assert client.deleted == [(STRANGER, [42], True)]
    assert dispatcher.events == []


def test_an_unauthorized_message_can_be_ignored_without_touching_telegram(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_WHITELIST_ONLY))
    _run(svc.set_policy(svc.POLICY_IGNORE))

    _run(handler(_event(STRANGER, STRANGER)))

    assert client.deleted == [] and client.sent == []
    assert dispatcher.events == []


def test_an_auto_reply_is_sent_once_per_sender(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_policy(svc.POLICY_AUTOREPLY))
    _run(svc.set_autoreply_text("I am away — I will read this later."))

    _run(handler(_event(STRANGER, STRANGER, message_id=1)))
    _run(handler(_event(STRANGER, STRANGER, message_id=2)))

    assert client.sent == [(STRANGER, "I am away — I will read this later.")]
    assert dispatcher.events == []


def test_an_authorized_sender_still_reaches_the_existing_handlers(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_policy(svc.POLICY_DELETE))
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
    _run(svc.set_policy(svc.POLICY_DELETE))
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
    _run(svc.set_policy(svc.POLICY_DELETE))

    _run(handler(_event(STRANGER, STRANGER, message_id=9)))  # must not raise

    assert dispatcher.events == []


def test_enforcement_applies_without_any_task_dispatcher_or_resolver(registered, db):
    client = _FakeSelfClient()
    task_events.register(client, OWNER, "Asia/Tehran")
    handler = client.handlers[0]
    assert task_events.get_dispatcher() is None and task_events.get_answer_resolver() is None
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_policy(svc.POLICY_DELETE))

    _run(handler(_event(STRANGER, STRANGER, message_id=11)))

    assert client.deleted == [(STRANGER, [11], True)]


def test_group_messages_are_outside_this_feature(registered, db):
    client = _FakeSelfClient()
    dispatcher = _RecordingDispatcher()
    handler = _register_event_handler(client, dispatcher)
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_policy(svc.POLICY_DELETE))

    event = _event(-1001234567890, STRANGER, message_id=13)
    event.is_private = False
    _run(handler(event))

    assert client.deleted == []
    assert len(dispatcher.events) == 1


def test_the_decision_seen_by_the_handler_is_the_persisted_one(registered, db):
    """The handler enforces what the DATABASE holds after a restart, not a default."""
    _run(svc.set_mode(svc.MODE_CLOSED))
    _run(svc.set_policy(svc.POLICY_DELETE))
    svc.reset_in_memory()
    _run(svc.ensure_loaded_async())

    client = _FakeSelfClient()
    handler = _register_event_handler(client, _RecordingDispatcher())
    _run(handler(_event(STRANGER, STRANGER, message_id=17)))

    assert client.deleted == [(STRANGER, [17], True)]
