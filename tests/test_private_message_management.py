"""Private Message Management — Phase 1 policy engine (panel-first, no AI).

Drives the REAL ``backend/services/private_message_service`` module: the
persisted ``bot_settings`` document, the validated mutations, the deterministic
effective-mode resolution (temporary → schedule → manual) and the incoming
message decision. Everything runs offline: the database is a fake
``bot_settings`` table and time is passed IN, so no wall clock, no Telegram and
no AI are involved.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services import private_message_service as svc

OWNER = 7770001
STRANGER = 5001
FRIEND = 5002
#: A Friday (weekday 4) — used as the fixed reference instant.
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeBotSettingsDB:
    """Chainable fake for the exact ``bot_settings`` access chains used here."""

    def __init__(self, rows: dict[str, str] | None = None, fail_writes: bool = False):
        self.rows = dict(rows or {})
        self.fail_writes = fail_writes
        self.inserts: list[dict] = []
        self.updates: list[dict] = []
        self._key: str | None = None
        self._selecting_value = False
        self._single = False
        self._pending_update: dict | None = None
        self._pending_insert: dict | None = None

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
        if self.fail_writes:
            raise RuntimeError("write rejected")
        self._pending_update = payload
        return self

    def insert(self, payload):
        if self.fail_writes:
            raise RuntimeError("write rejected")
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


@pytest.fixture(autouse=True)
def _clean_state():
    svc.reset_in_memory()
    yield
    svc.reset_in_memory()


@pytest.fixture()
def db(monkeypatch):
    fake = _FakeBotSettingsDB()
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    return fake


@pytest.fixture()
def no_db(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)


def _state(**overrides) -> dict:
    state = svc.sanitize_state({})
    state.update(overrides)
    return state


def _decide(state_doc, *, sender=STRANGER, private=True, at=NOW, owner=False):
    return svc.decide_incoming(
        state_doc, sender_id=sender, is_private=private, is_owner=owner, now=at
    )


def _rule(days, start, end, mode):
    return {"days": days, "start": start, "end": end, "mode": mode}


# ── default / backward compatibility ─────────────────────────────────────────


def test_absent_configuration_is_open_and_performs_no_policy_action():
    state = svc.state()
    assert state["mode"] == svc.MODE_OPEN
    assert state["policy"] == svc.POLICY_IGNORE
    assert state["allowlist"] == [] and state["exceptions"] == []
    assert state["temporary"] is None
    decision = _decide(state)
    assert decision.permitted is True
    assert decision.action == svc.ACTION_ALLOW
    assert decision.reason == "open mode"
    assert svc.summarize(state, NOW).mode == svc.MODE_OPEN


def test_a_malformed_stored_document_falls_back_to_safe_defaults():
    state = svc.sanitize_state(
        {
            "enabled": "yes",
            "mode": "blocked",
            "policy": "nuke",
            "allowlist": ["ali", -3, True, 42, 42],
            "temporary": {"mode": "closed"},
            "schedule": {"enabled": True, "timezone": "Mars/Olympus", "rules": [{"days": []}]},
        }
    )
    assert state["enabled"] is True  # the invalid value never disables enforcement
    assert state["mode"] == svc.MODE_OPEN
    assert state["policy"] == svc.POLICY_IGNORE
    assert state["allowlist"] == [42]
    assert state["temporary"] is None
    assert state["schedule"]["timezone"] == svc.default_timezone()
    assert state["schedule"]["rules"] == []


def test_garbage_json_in_the_row_is_ignored(db):
    db.rows[svc.STATE_ROW_KEY] = "{not json"
    asyncio.run(svc.ensure_loaded_async())
    assert svc.state()["mode"] == svc.MODE_OPEN


# ── modes: open / whitelist_only / closed ────────────────────────────────────


def test_open_mode_permits_everyone():
    state = _state(mode=svc.MODE_OPEN, policy=svc.POLICY_DELETE)
    decision = _decide(state)
    assert decision.permitted is True and decision.action == svc.ACTION_ALLOW


def test_whitelist_only_permits_the_allowlist_and_restricts_others():
    state = _state(mode=svc.MODE_WHITELIST_ONLY, policy=svc.POLICY_DELETE, allowlist=[FRIEND])
    allowed = _decide(state, sender=FRIEND)
    denied = _decide(state, sender=STRANGER)
    assert allowed.permitted is True and allowed.reason == "authorized sender"
    assert denied.permitted is False and denied.action == svc.POLICY_DELETE
    assert denied.mode == svc.MODE_WHITELIST_ONLY


def test_closed_mode_permits_only_the_exceptions():
    state = _state(mode=svc.MODE_CLOSED, policy=svc.POLICY_IGNORE, exceptions=[FRIEND], allowlist=[STRANGER])
    assert _decide(state, sender=FRIEND).permitted is True
    assert _decide(state, sender=STRANGER).permitted is False


def test_group_messages_and_the_owner_are_never_restricted():
    state = _state(mode=svc.MODE_CLOSED, policy=svc.POLICY_DELETE)
    assert _decide(state, sender=STRANGER, private=False).permitted is True
    assert _decide(state, sender=OWNER, owner=True).permitted is True


def test_the_master_switch_disables_every_policy_action():
    state = _state(enabled=False, mode=svc.MODE_CLOSED, policy=svc.POLICY_DELETE)
    decision = _decide(state)
    assert decision.permitted is True
    assert decision.reason == "feature disabled"
    assert decision.mode == svc.MODE_CLOSED  # the configured mode is still shown


def test_an_unauthorized_sender_with_an_unusable_id_is_restricted_not_permitted():
    state = _state(mode=svc.MODE_CLOSED, policy=svc.POLICY_IGNORE)
    assert _decide(state, sender=None).permitted is False
    assert _decide(state, sender=True).permitted is False
    assert _decide(state, sender=0).permitted is False


# ── unauthorized policy ──────────────────────────────────────────────────────


def test_autoreply_without_text_falls_back_to_ignoring():
    state = _state(mode=svc.MODE_CLOSED, policy=svc.POLICY_AUTOREPLY, autoreply_text="   ")
    decision = _decide(state)
    assert decision.permitted is False
    assert decision.action == svc.POLICY_IGNORE
    assert decision.autoreply_text == ""


def test_autoreply_text_travels_with_the_decision():
    state = _state(mode=svc.MODE_CLOSED, policy=svc.POLICY_AUTOREPLY, autoreply_text="  busy  ")
    decision = _decide(state)
    assert decision.action == svc.POLICY_AUTOREPLY
    assert decision.autoreply_text == "busy"


def test_autoreply_slots_are_rate_limited_per_sender_and_pruned():
    svc.reset_in_memory()
    assert svc.consume_autoreply_slot(STRANGER, 1000.0) is True
    assert svc.consume_autoreply_slot(STRANGER, 1000.5) is False
    assert svc.consume_autoreply_slot(OTHER_SENDER := STRANGER + 1, 1000.5) is True
    assert svc.autoreply_seconds_remaining(STRANGER, 1010.0) == int(svc.AUTOREPLY_COOLDOWN_SECONDS - 10)
    # Past the cooldown the slot is free again, and the mapping is pruned.
    later = 1000.0 + svc.AUTOREPLY_COOLDOWN_SECONDS + 1
    assert svc.consume_autoreply_slot(STRANGER, later) is True
    assert OTHER_SENDER not in svc._autoreply_slots


# ── mutations + persistence ──────────────────────────────────────────────────


def test_mutations_insert_then_update_the_bot_settings_row(db):
    assert asyncio.run(svc.set_mode(svc.MODE_CLOSED)) is True
    assert db.inserts and db.inserts[0]["key"] == svc.STATE_ROW_KEY
    assert db.inserts[0]["value_type"] == "str"
    assert json.loads(db.inserts[0]["value"])["mode"] == svc.MODE_CLOSED
    assert asyncio.run(svc.set_policy(svc.POLICY_DELETE)) is True
    assert db.updates and db.updates[-1]["key"] == svc.STATE_ROW_KEY
    stored = json.loads(db.updates[-1]["value"])
    assert stored["mode"] == svc.MODE_CLOSED and stored["policy"] == svc.POLICY_DELETE


def test_allowlist_add_and_remove_round_trip_through_the_row(db):
    assert asyncio.run(svc.add_list_entry("allowlist", FRIEND)) is True
    assert asyncio.run(svc.add_list_entry("allowlist", STRANGER)) is True
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["allowlist"] == sorted([FRIEND, STRANGER])
    assert asyncio.run(svc.remove_list_entry("allowlist", FRIEND)) is True
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["allowlist"] == [STRANGER]
    assert svc.state()["allowlist"] == [STRANGER]


def test_the_effective_policy_survives_a_restart(db):
    assert asyncio.run(svc.set_mode(svc.MODE_CLOSED)) is True
    assert asyncio.run(svc.set_policy(svc.POLICY_DELETE)) is True
    assert asyncio.run(svc.add_list_entry("exceptions", FRIEND)) is True

    svc.reset_in_memory()  # a fresh process: nothing in memory, one row in the DB
    assert svc.state()["mode"] == svc.MODE_OPEN  # safe default until the load lands

    asyncio.run(svc.ensure_loaded_async())
    restored = svc.state()
    assert restored["mode"] == svc.MODE_CLOSED
    assert restored["policy"] == svc.POLICY_DELETE
    assert restored["exceptions"] == [FRIEND]
    assert _decide(restored, sender=STRANGER).action == svc.POLICY_DELETE
    assert _decide(restored, sender=FRIEND).permitted is True


def test_concurrent_mutations_are_serialized(db):
    async def _both():
        return await asyncio.gather(
            svc.add_list_entry("allowlist", FRIEND),
            svc.add_list_entry("allowlist", STRANGER),
        )

    assert asyncio.run(_both()) == [True, True]
    assert svc.state()["allowlist"] == sorted([FRIEND, STRANGER])
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["allowlist"] == sorted([FRIEND, STRANGER])


def test_a_failed_write_leaves_the_state_unchanged(monkeypatch):
    fake = _FakeBotSettingsDB(fail_writes=True)
    monkeypatch.setattr("backend.db.client.get_db", lambda: fake)
    assert asyncio.run(svc.set_mode(svc.MODE_CLOSED)) is False
    assert svc.state()["mode"] == svc.MODE_OPEN


def test_without_a_database_the_state_is_memory_only_and_the_panel_text_says_so(no_db):
    assert svc.persistence_available() is False
    assert asyncio.run(svc.set_mode(svc.MODE_CLOSED)) is True
    assert svc.state()["mode"] == svc.MODE_CLOSED


# ── validation ───────────────────────────────────────────────────────────────


def test_invalid_settings_are_rejected_and_change_nothing(db):
    before = svc.state()
    assert asyncio.run(svc.set_mode("blocked")) is False
    assert asyncio.run(svc.set_policy("nuke")) is False
    assert asyncio.run(svc.set_autoreply_text("")) is False
    assert asyncio.run(svc.set_autoreply_text("x" * (svc.MAX_AUTOREPLY_LENGTH + 1))) is False
    assert asyncio.run(svc.set_schedule_timezone("Mars/Olympus")) is False
    assert asyncio.run(svc.add_list_entry("allowlist", -5)) is False
    assert asyncio.run(svc.add_list_entry("allowlist", "ali")) is False
    assert asyncio.run(svc.add_list_entry("exceptions", 0)) is False
    assert asyncio.run(svc.add_list_entry("nope", FRIEND)) is False
    assert asyncio.run(svc.set_temporary("closed", 0)) is False
    assert asyncio.run(svc.set_temporary("closed", 60 * 24 * 8)) is False
    assert asyncio.run(svc.set_temporary("nope", 30)) is False
    assert asyncio.run(svc.save_schedule_rule(_rule([0], "09:00", "09:00", "closed"))) is False
    assert asyncio.run(svc.save_schedule_rule(_rule([], "09:00", "18:00", "closed"))) is False
    assert asyncio.run(svc.save_schedule_rule(_rule([0], "9:0", "18:00", "closed"))) is False
    assert asyncio.run(svc.save_schedule_rule(_rule([0], "09:00", "18:00", "blocked"))) is False
    assert svc.state() == before
    assert db.rows == {}


def test_schedule_rules_can_be_edited_deleted_and_are_bounded(db):
    assert asyncio.run(svc.save_schedule_rule(_rule([0], "09:00", "18:00", "closed"))) is True
    assert asyncio.run(svc.save_schedule_rule(_rule([1, 2], "10:00", "11:00", "whitelist_only"))) is True
    assert svc.state()["schedule"]["rules"][1]["start"] == "10:00"

    assert asyncio.run(
        svc.save_schedule_rule(_rule([3], "07:00", "08:00", "closed"), index=0)
    ) is True
    rules = svc.state()["schedule"]["rules"]
    assert rules[0]["days"] == [3] and rules[0]["start"] == "07:00"
    assert len(rules) == 2 and rules[0]["id"] != rules[1]["id"]

    assert asyncio.run(svc.delete_schedule_rule(0)) is True
    assert len(svc.state()["schedule"]["rules"]) == 1
    assert asyncio.run(svc.delete_schedule_rule(9)) is False

    remaining = svc.MAX_SCHEDULE_RULES - len(svc.state()["schedule"]["rules"])
    for index in range(remaining):
        assert asyncio.run(svc.save_schedule_rule(_rule([0], "%02d:00" % index, "23:00", "closed"))) is True
    assert len(svc.state()["schedule"]["rules"]) == svc.MAX_SCHEDULE_RULES
    assert asyncio.run(svc.save_schedule_rule(_rule([0], "06:00", "07:00", "closed"))) is False


# ── temporary override ───────────────────────────────────────────────────────


def test_a_temporary_override_activates_with_an_expiry_in_the_future(db):
    assert asyncio.run(svc.set_temporary(svc.MODE_CLOSED, 30)) is True
    temporary = svc.state()["temporary"]
    activated = svc.parse_instant(temporary["activated_at"])
    expires = svc.parse_instant(temporary["expires_at"])
    assert timedelta(minutes=30) - timedelta(seconds=5) < (expires - activated) <= timedelta(minutes=30)
    policy = svc.resolve_policy(svc.state(), datetime.now(timezone.utc))
    assert policy.mode == svc.MODE_CLOSED and policy.source == svc.SOURCE_TEMPORARY
    assert policy.remaining_seconds(datetime.now(timezone.utc)) > 0


def test_an_expired_temporary_override_falls_back_to_schedule_then_manual():
    expires = NOW + timedelta(minutes=15)
    state = _state(
        mode=svc.MODE_OPEN,
        schedule={"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "00:00", "23:00", "whitelist_only")]},
        temporary={"mode": svc.MODE_CLOSED, "activated_at": NOW.isoformat(), "expires_at": expires.isoformat()},
    )
    during = svc.resolve_policy(state, NOW + timedelta(minutes=1))
    assert during.mode == svc.MODE_CLOSED and during.source == svc.SOURCE_TEMPORARY
    after = svc.resolve_policy(state, expires + timedelta(seconds=1))
    assert after.mode == svc.MODE_WHITELIST_ONLY and after.source == svc.SOURCE_SCHEDULE
    # The stored record is never trusted past its expiry.
    assert svc.temporary_mode(state, expires) is None


def test_expired_temporary_override_is_persisted_away(db):
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    svc._state["temporary"] = {
        "mode": svc.MODE_CLOSED,
        "activated_at": (past - timedelta(minutes=30)).isoformat(),
        "expires_at": past.isoformat(),
    }
    assert asyncio.run(svc.drop_expired_temporary()) is True
    assert svc.state()["temporary"] is None
    assert json.loads(db.rows[svc.STATE_ROW_KEY])["temporary"] is None
    # Nothing left to drop.
    assert asyncio.run(svc.drop_expired_temporary()) is False


def test_manual_mode_has_no_expiry_and_changes_only_when_changed(db):
    assert asyncio.run(svc.set_mode(svc.MODE_CLOSED)) is True
    policy = svc.resolve_policy(svc.state(), NOW + timedelta(days=365))
    assert policy.mode == svc.MODE_CLOSED and policy.source == svc.SOURCE_MANUAL
    assert policy.expires_at is None and policy.remaining_seconds(NOW) is None


# ── schedule ─────────────────────────────────────────────────────────────────


def test_schedule_matches_by_day_and_explicit_timezone():
    schedule = {
        "enabled": True,
        "timezone": "Asia/Tehran",
        "rules": [_rule([1, 2, 3, 4, 5], "09:00", "18:00", "whitelist_only")],
    }
    # Saturday 2026-10-17 10:00 Tehran (06:30 UTC) is inside the window.
    saturday = datetime(2026, 10, 17, 6, 30, tzinfo=timezone.utc)
    assert svc.schedule_mode(schedule, saturday) == svc.MODE_WHITELIST_ONLY
    # Sunday 2026-10-18 10:00 Tehran is the same wall clock on a day not listed.
    assert svc.schedule_mode(schedule, datetime(2026, 10, 18, 6, 30, tzinfo=timezone.utc)) is None
    # Saturday 19:00 Tehran is past the end.
    assert svc.schedule_mode(schedule, datetime(2026, 10, 17, 15, 30, tzinfo=timezone.utc)) is None
    # The SAME UTC instant in a different timezone is a different local time.
    utc_rule = {"enabled": True, "timezone": "UTC", "rules": [_rule([5], "09:00", "18:00", "closed")]}
    assert svc.schedule_mode(utc_rule, datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)) == svc.MODE_CLOSED


def test_an_overnight_window_belongs_to_the_day_it_starts():
    schedule = {
        "enabled": True,
        "timezone": "UTC",
        "rules": [_rule([5], "22:00", "06:00", "closed")],  # Saturday night
    }
    assert svc.schedule_mode(schedule, datetime(2026, 10, 17, 21, 30, tzinfo=timezone.utc)) is None
    assert svc.schedule_mode(schedule, datetime(2026, 10, 17, 22, 30, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 10, 18, 5, 59, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)) is None
    # Monday morning is not part of a Saturday overnight window.
    assert svc.schedule_mode(schedule, datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)) is None


def test_a_disabled_schedule_applies_nothing():
    schedule = {"enabled": False, "timezone": "UTC", "rules": [_rule(list(range(7)), "00:00", "23:59", "closed")]}
    assert svc.schedule_mode(schedule, NOW) is None


def test_the_last_matching_rule_wins():
    schedule = {
        "enabled": True,
        "timezone": "UTC",
        "rules": [
            _rule(list(range(7)), "00:00", "23:00", "whitelist_only"),
            _rule(list(range(7)), "08:00", "20:00", "closed"),
        ],
    }
    assert svc.schedule_mode(schedule, datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)) == svc.MODE_WHITELIST_ONLY


def test_dst_boundaries_stay_deterministic():
    # 2026-11-01 is the US DST end date: 01:00–03:00 local happens over three
    # UTC hours, resolved by the local wall clock (zoneinfo fold rules).
    schedule = {
        "enabled": True,
        "timezone": "America/New_York",
        "rules": [_rule([6], "01:00", "03:00", "closed")],
    }
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)) is None
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)) == svc.MODE_CLOSED
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)) is None
    # Determinism: the same instant always resolves the same way.
    assert svc.schedule_mode(schedule, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == svc.MODE_CLOSED


# ── precedence ───────────────────────────────────────────────────────────────


def test_precedence_temporary_then_schedule_then_manual():
    schedule = {"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "00:00", "23:59", "whitelist_only")]}
    state = _state(mode=svc.MODE_OPEN, schedule=schedule)
    assert svc.resolve_mode(state, NOW) == (svc.MODE_WHITELIST_ONLY, svc.SOURCE_SCHEDULE)

    state["temporary"] = {
        "mode": svc.MODE_CLOSED,
        "activated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    assert svc.resolve_mode(state, NOW) == (svc.MODE_CLOSED, svc.SOURCE_TEMPORARY)

    state["schedule"]["enabled"] = False
    state["temporary"] = None
    assert svc.resolve_mode(state, NOW) == (svc.MODE_OPEN, svc.SOURCE_MANUAL)


def test_the_next_transition_names_the_real_change():
    state = _state(
        mode=svc.MODE_CLOSED,
        schedule={"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "18:00", "20:00", "whitelist_only")]},
    )
    at, label = svc.next_transition(state, datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc))
    assert at == datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
    assert svc.MODE_LABELS[svc.MODE_WHITELIST_ONLY] in label

    # A boundary that changes nothing is not reported: the manual mode is
    # already the mode the rule would apply, and it is what closing the rule
    # returns to.
    same = _state(
        mode=svc.MODE_CLOSED,
        schedule={"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "18:00", "20:00", "closed")]},
    )
    assert svc.next_transition(same, datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)) == (None, "")


def test_a_schedule_boundary_hidden_under_a_temporary_override_is_not_the_next_transition():
    expires = NOW + timedelta(hours=2)
    state = _state(
        enabled=True,
        mode=svc.MODE_OPEN,
        schedule={"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "12:30", "13:30", "closed")]},
        temporary={"mode": svc.MODE_WHITELIST_ONLY, "activated_at": NOW.isoformat(), "expires_at": expires.isoformat()},
    )
    at, label = svc.next_transition(state, NOW)
    assert at == expires
    assert "temporary override ends" in label


def test_no_transition_is_promised_when_enforcement_is_off_or_nothing_is_scheduled():
    assert svc.next_transition(_state(enabled=False, mode=svc.MODE_CLOSED), NOW) == (None, "")
    assert svc.next_transition(_state(mode=svc.MODE_OPEN), NOW) == (None, "")
    # A window that has already closed today: the next real change is the
    # same rule opening tomorrow.
    stopped = _state(
        mode=svc.MODE_OPEN,
        schedule={"enabled": True, "timezone": "UTC", "rules": [_rule(list(range(7)), "13:00", "14:00", "closed")]},
    )
    at, label = svc.next_transition(stopped, datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc))
    assert at == datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
    assert svc.MODE_LABELS[svc.MODE_CLOSED] in label


# ── summary / formatting helpers used by the panel ───────────────────────────


def test_summary_reports_the_effective_state_the_panel_renders():
    state = _state(
        mode=svc.MODE_CLOSED,
        policy=svc.POLICY_AUTOREPLY,
        autoreply_text="busy",
        allowlist=[FRIEND],
        schedule={"enabled": True, "timezone": "Asia/Tehran", "rules": [_rule([4], "09:00", "18:00", "open")]},
    )
    summary = svc.summarize(state, NOW)  # NOW is Friday 15:30 Tehran → inside the rule
    assert summary.mode == svc.MODE_OPEN and summary.source == svc.SOURCE_SCHEDULE
    assert summary.enabled is True
    assert summary.policy == svc.POLICY_AUTOREPLY and summary.autoreply_text == "busy"
    assert summary.allowlist == (FRIEND,)
    assert summary.schedule_enabled is True and summary.schedule_timezone == "Asia/Tehran"
    assert len(summary.schedule_rules) == 1


def test_formatting_helpers_are_total():
    assert svc.format_remaining(None) == "—"
    assert svc.format_remaining(0) == "expiring now"
    assert svc.format_remaining(59) == "59s"
    assert svc.format_remaining(90) == "1m 30s"
    assert svc.format_remaining(3660) == "1h 01m"
    assert svc.format_instant(None) == "—"
    assert "Tehran" in svc.format_instant(NOW, "Asia/Tehran") or "+0330" in svc.format_instant(NOW, "Asia/Tehran")
    assert svc.describe_days([0, 1, 2, 3, 4]) == "Mon–Fri"
    assert svc.describe_days(list(range(7))) == "Every day"
    assert svc.describe_days([]) == "—"
    assert "Mon" in svc.describe_days([0, 3]) and "Thu" in svc.describe_days([0, 3])
    assert "22:00–06:00" in svc.describe_rule(_rule([5], "22:00", "06:00", "closed"))
    assert svc.parse_instant("nope") is None
    assert svc.parse_instant("2026-10-16T12:00:00") == NOW
    assert svc.parse_hhmm("24:00") is None and svc.parse_hhmm("23:59") is not None
    assert svc.resolve_zone("Not/AZone") is None
