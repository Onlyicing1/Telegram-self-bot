"""TelegramAPI.get_bio — authoritative full-profile bio retrieval.

Verifies the facade → entity-layer delegation and that the entity layer
uses Telethon's ``GetFullUserRequest`` (full_user.about), NOT the basic
``get_me`` user object, and extracts only the bio text.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_client(full_about: str | None) -> MagicMock:
    """A fake Telethon client whose get_me + GetFullUserRequest behave like
    the real RPC pair. ``client(request)`` is the Telethon call syntax."""
    client = MagicMock()
    me = SimpleNamespace(id=999, first_name="Ali", about=None)
    full_requests: list[object] = []

    async def fake_get_me():
        return me

    async def fake_call(request):
        full_requests.append(request)
        return SimpleNamespace(
            full_user=SimpleNamespace(about=full_about),
            users=[me],
        )

    client.get_me = AsyncMock(side_effect=fake_get_me)
    client.side_effect = fake_call
    client._full_requests = full_requests
    return client


@pytest.mark.asyncio
async def test_get_bio_uses_full_user_request_and_extracts_about():
    from backend.telegram_api import entities

    client = _make_client("همیشه بهروز")
    bio = await entities.get_bio(client)

    assert bio == "همیشه بهروز"
    # The authoritative full-profile request was used exactly once.
    assert len(client._full_requests) == 1
    assert client._full_requests[0].__class__.__name__ == "GetFullUserRequest"


@pytest.mark.asyncio
async def test_get_bio_empty_authoritative_bio_is_empty_string():
    from backend.telegram_api import entities

    client = _make_client(None)
    bio = await entities.get_bio(client)
    assert bio == ""


@pytest.mark.asyncio
async def test_get_bio_does_not_call_basic_get_me_serialization_path():
    # The bio must come from full_user.about, not serialize_user(get_me()).
    from backend.telegram_api import entities

    client = _make_client("full-profile bio")
    # get_me result deliberately carries NO 'about'; if the code read the
    # basic user object the result would be empty instead.
    await entities.get_bio(client)
    assert client.get_me.await_count == 1  # only to address the full request


@pytest.mark.asyncio
async def test_facade_get_bio_delegates_to_entities():
    from backend.telegram_api import api as api_module
    from backend.telegram_api.api import TelegramAPI

    client = _make_client("facade bio")
    facade = TelegramAPI(client)
    with patch.object(api_module.entities, "get_bio", new=AsyncMock(return_value="facade bio")) as mocked:
        bio = await facade.get_bio()

    assert bio == "facade bio"
    mocked.assert_awaited_once_with(client)


@pytest.mark.asyncio
async def test_tool_layer_never_sees_userfull_object():
    """BioGetTool must receive only the bio string — never the full UserFull."""
    from backend.ai.tools.bio import BioGetTool
    from backend.ai.tools.context import ToolContext

    class FakeTelegram:
        def __init__(self):
            self.calls: list[str] = []

        async def get_bio(self):
            self.calls.append("get_bio")
            return "only-the-bio"

        async def get_me(self):  # must NOT be called by the bio tool
            self.calls.append("get_me")
            return {"about": "wrong-source"}

    fake = FakeTelegram()
    ctx = ToolContext(telegram=fake, owner_id=1, tz_str="UTC")
    result = await BioGetTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data == {"bio": "only-the-bio"}
    assert fake.calls == ["get_bio"]
