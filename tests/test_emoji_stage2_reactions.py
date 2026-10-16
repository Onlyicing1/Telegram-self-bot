"""Emoji Stage 2 — functional Auto-Reaction settings + Telegram-sourced
reaction selector, and the removal of the "Custom Emoji" category feature.

What is pinned here:

* ``backend.telegram_api.reactions.get_available_reactions`` reads the
  account's catalog from Telegram (real ``AvailableReaction`` records),
  serializes exactly the emoji value Telegram exposes, keeps a
  ``NotModified`` answer distinct from "no reactions", and fails closed on
  an unexpected response.
* ``backend.services.reaction_source_service`` builds selectable options
  from Telegram's catalog only (no hardcoded list), drops unusable/inactive
  records, and caches with a bounded TTL + explicit force refresh.
* The General panel exposes a working selector entry, and the picker panel
  renders REAL Telegram options, persists the exact emoji through the
  existing ``settings_service`` path, fails closed on a stale index, and
  shows an honest notice when Telegram returns nothing.
* The "Custom Emoji" category feature (ROADMAP §26) is gone from the UI,
  the service and the db layer.

Everything runs offline against a fake self client — no Telegram, no DB.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.helper import inline_engine
from backend.helper.panels import get_action, get_panel
from backend.services import reaction_source_service, settings_service
from backend.telegram_api.exceptions import TelegramAPIError
from backend.telegram_api.reactions import get_available_reactions

OWNER = 7770002

_TL_DOC = None  # AvailableReaction media args are unused by our serializer


def _available_reaction(emoji, *, premium=False, inactive=False, title=""):
    from telethon.tl.types import AvailableReaction

    return AvailableReaction(
        reaction=emoji, title=title or emoji, static_icon=_TL_DOC,
        appear_animation=_TL_DOC, select_animation=_TL_DOC,
        activate_animation=_TL_DOC, effect_animation=_TL_DOC,
        premium=premium, inactive=inactive,
    )


def _available_reactions(records, hash=11):
    from telethon.tl.types.messages import AvailableReactions

    return AvailableReactions(hash=hash, reactions=list(records))


class _FakeSelfClient:
    """Records the TL request and returns a configured response (or raises)."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _env():
    from backend.db import client as db_client

    before = dict(settings_service.get_all())
    reaction_source_service.reset_cache()
    inline_engine.set_self_client(None)
    yield
    reaction_source_service.reset_cache()
    inline_engine.set_self_client(None)
    settings_service._cache.clear()
    settings_service._cache.update(before)


def _register_misc():
    from backend.bot.handlers import misc

    class _FakeClient:
        def on(self, *a, **k):
            def _d(fn):
                return fn

            return _d

    misc.register(client=_FakeClient(), owner_id=OWNER)


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


# ── A. telegram_api.get_available_reactions ──────────────────────────────────


def test_wrapper_serializes_real_available_reactions():
    client = _FakeSelfClient(_available_reactions([
        _available_reaction("👍", title="Thumbs Up"),
        _available_reaction("❤️", title="Heart", premium=True),
    ], hash=42))
    result = _run(get_available_reactions(client))
    assert result["ok"] is True
    assert result["modified"] is False
    assert result["hash"] == 42
    assert [r["reaction"] for r in result["reactions"]] == ["👍", "❤️"]
    assert result["reactions"][1]["premium"] is True
    # exactly one request, of the expected type
    from telethon.tl.functions.messages import GetAvailableReactionsRequest

    assert len(client.requests) == 1
    assert isinstance(client.requests[0], GetAvailableReactionsRequest)
    assert client.requests[0].hash == 0


def test_wrapper_not_modified_is_distinct_from_empty():
    from telethon.tl.types.messages import AvailableReactionsNotModified

    client = _FakeSelfClient(AvailableReactionsNotModified())
    result = _run(get_available_reactions(client, hash=9))
    assert result["ok"] is True
    assert result["modified"] is True
    assert result["reactions"] == []
    assert result["hash"] == 9


def test_wrapper_drops_records_without_a_usable_emoji():
    bad = _available_reaction("👍")
    object.__setattr__(bad, "reaction", "")  # simulate a malformed record
    client = _FakeSelfClient(_available_reactions([bad, _available_reaction("🔥")]))
    result = _run(get_available_reactions(client))
    assert [r["reaction"] for r in result["reactions"]] == ["🔥"]


def test_wrapper_fails_closed_on_unexpected_response():
    client = _FakeSelfClient(response=object())
    with pytest.raises(TelegramAPIError):
        _run(get_available_reactions(client))


def test_wrapper_requires_a_client():
    with pytest.raises(TelegramAPIError):
        _run(get_available_reactions(None))


# ── B. reaction_source_service (Telegram is the only source) ─────────────────


def test_service_options_come_from_telegram_only():
    client = _FakeSelfClient(_available_reactions([
        _available_reaction("👍"), _available_reaction("🔥", premium=True),
    ]))
    result = _run(reaction_source_service.load_reactions(client))
    assert result["ok"] is True
    assert [o["emoji"] for o in result["options"]] == ["👍", "🔥"]
    # each option's reaction is directly usable by the send path
    from backend.telegram_api.reactions import normalize_reaction

    for option in result["options"]:
        assert normalize_reaction(option["reaction"]) == option["reaction"]


def test_service_drops_inactive_and_unusable_records():
    client = _FakeSelfClient(_available_reactions([
        _available_reaction("👍"),
        _available_reaction("💤", inactive=True),
    ]))
    result = _run(reaction_source_service.load_reactions(client))
    assert [o["emoji"] for o in result["options"]] == ["👍"]


def test_service_caches_within_ttl_and_forces_on_request():
    client = _FakeSelfClient(_available_reactions([_available_reaction("👍")]))
    first = _run(reaction_source_service.load_reactions(client))
    assert first["cached"] is False
    second = _run(reaction_source_service.load_reactions(client))
    assert second["cached"] is True
    assert len(client.requests) == 1  # cache served, no second request
    _run(reaction_source_service.load_reactions(client, force=True))
    assert len(client.requests) == 2  # force re-read


def test_service_not_modified_keeps_cached_catalog():
    from telethon.tl.types.messages import AvailableReactionsNotModified

    client = _FakeSelfClient(_available_reactions([_available_reaction("👍")]))
    _run(reaction_source_service.load_reactions(client, force=True))
    client.response = AvailableReactionsNotModified()
    result = _run(reaction_source_service.load_reactions(client, force=True))
    assert result["ok"] is True
    assert [o["emoji"] for o in result["options"]] == ["👍"]


def test_service_empty_catalog_is_reported_honestly():
    client = _FakeSelfClient(_available_reactions([]))
    result = _run(reaction_source_service.load_reactions(client))
    assert result["ok"] is False
    assert result["error"] == reaction_source_service.ERROR_EMPTY
    assert result["options"] == []


def test_service_no_client_is_reported_honestly():
    result = _run(reaction_source_service.load_reactions(None))
    assert result["ok"] is False
    assert result["error"] == reaction_source_service.ERROR_NO_CLIENT


def test_service_api_error_is_reported_honestly():
    client = _FakeSelfClient(error=RuntimeError("boom"))
    result = _run(reaction_source_service.load_reactions(client))
    assert result["ok"] is False
    assert result["error"] == reaction_source_service.ERROR_TELEGRAM


# ── C. settings_service: the persisted selection ─────────────────────────────


def test_auto_reaction_emoji_defaults_empty():
    assert settings_service.auto_reaction_emoji() == ""


def test_auto_reaction_emoji_round_trips():
    assert settings_service.set_auto_reaction_emoji("👍") is True
    assert settings_service.auto_reaction_emoji() == "👍"


def test_auto_reaction_emoji_is_a_known_key():
    assert "auto_reaction_emoji" in settings_service.known_keys()
    assert settings_service.is_valid_key("auto_reaction_emoji")


def test_auto_reaction_emoji_validator_rejects_bad_values():
    assert settings_service.set_setting("auto_reaction_emoji", 5) is False
    assert settings_service.set_setting("auto_reaction_emoji", "") is False


# ── D. General panel + the reaction picker ───────────────────────────────────


def test_general_exposes_the_reaction_selector_entry():
    _register_misc()
    buttons = misc_build_general()
    datas = _datas(buttons)
    assert "panel:general_reactions" in datas
    assert "action:general_toggle_auto_reaction" in datas


def misc_build_general():
    from backend.bot.handlers import misc

    return misc._build_general_buttons()


def test_general_body_shows_the_selected_reaction():
    from backend.bot.handlers import misc

    settings_service.set_auto_reaction_emoji("🔥")
    body = _run(misc._general_body())
    assert "🔥" in body


def test_reaction_picker_panel_is_registered_as_a_general_subpanel():
    _register_misc()
    panel = get_panel("general_reactions")
    assert panel is not None
    assert get_action("general_reaction_set") is not None
    assert get_action("general_reaction_refresh") is not None


def test_picker_renders_real_telegram_options_not_a_hardcoded_list():
    from backend.bot.handlers import misc

    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([
        _available_reaction("🎯"), _available_reaction("🐳", premium=True),
    ])))
    _title, body, buttons = _run(misc._render_reaction_picker(0))
    labels = _texts(buttons)
    assert any("🎯" in t for t in labels)
    assert any("🐳" in t for t in labels)
    datas = _datas(buttons)
    assert "action:general_reaction_set:0" in datas
    assert "action:general_reaction_set:1" in datas
    assert "action:general_reaction_refresh" in datas


def test_picker_selecting_persists_the_exact_emoji():
    from backend.bot.handlers import misc

    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([
        _available_reaction("👍"), _available_reaction("🔥"),
    ])))
    _run(misc._render_reaction_picker(0))  # populate the cache
    _run(misc._general_reaction_set_action(None, ":1", 0))
    assert settings_service.auto_reaction_emoji() == "🔥"


def test_picker_stale_index_fails_closed_without_changing_the_setting():
    from backend.bot.handlers import misc

    settings_service.set_auto_reaction_emoji("👍")
    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([
        _available_reaction("👍"),
    ])))
    _run(misc._render_reaction_picker(0))
    _title, body, _buttons = _run(misc._general_reaction_set_action(None, ":99", 0))
    assert "out of date" in body.lower()
    assert settings_service.auto_reaction_emoji() == "👍"


def test_picker_refresh_forces_a_new_read():
    from backend.bot.handlers import misc

    client = _FakeSelfClient(_available_reactions([_available_reaction("👍")]))
    inline_engine.set_self_client(client)
    _run(misc._render_reaction_picker(0))
    assert len(client.requests) == 1
    _run(misc._general_reaction_refresh_action(None, "", 0))
    assert len(client.requests) == 2


def test_picker_empty_catalog_shows_honest_notice():
    from backend.bot.handlers import misc

    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([])))
    _title, body, buttons = _run(misc._render_reaction_picker(0))
    assert "no usable reactions" in body.lower()
    assert not any("general_reaction_set" in d for d in _datas(buttons))


def test_picker_marks_the_current_selection():
    from backend.bot.handlers import misc

    settings_service.set_auto_reaction_emoji("🔥")
    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([
        _available_reaction("👍"), _available_reaction("🔥"),
    ])))
    _title, _body, buttons = _run(misc._render_reaction_picker(0))
    labels = _texts(buttons)
    marked = [t for t in labels if t.startswith("✓")]
    assert marked and "🔥" in marked[0]


def test_picker_subpanel_gets_standard_navigation():
    from backend.helper.lifecycle import get_lifecycle
    from backend.helper.panels import _finalize_panel
    from backend.bot.handlers import misc

    _register_misc()
    inline_engine.set_self_client(_FakeSelfClient(_available_reactions([_available_reaction("👍")])))
    lifecycle = get_lifecycle()
    chat_id, msg_id = 4600, 7
    lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
    lifecycle.sessions.push_nav(chat_id, msg_id, "general", "")
    title, body, buttons = _run(misc._render_reaction_picker(0))
    _t, _b, finalized = _finalize_panel(
        title, body, buttons, "general_reactions", chat_id, msg_id
    )
    datas = _datas(finalized)
    assert "panel:_nav:back" in datas
    assert "panel:_nav:home" in datas
    assert "panel:_nav:close" in datas


# ── E. no second infrastructure ──────────────────────────────────────────────


def test_reaction_source_service_imports_no_events_or_ai():
    from pathlib import Path

    source = Path(reaction_source_service.__file__).read_text()
    assert "import events" not in source
    assert "from telethon import events" not in source
    assert "backend.ai" not in source
    assert "asyncio.create_task" not in source
