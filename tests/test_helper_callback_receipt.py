"""
Helper-bot callback receipt diagnostic.

The hook registered by ``register_helper_hooks`` is the EARLIEST
self-observable point of any helper-bot callback query: it runs before the
router's owner/coordinates/data/session gates, so it records ONE bounded line
per delivered callback (data + sender + message coordinates). That line is
what makes "this click was never delivered" distinguishable from "delivered
and dropped before the router's first unconditional log" — the exact blind
spot of ``last_callback_age``, which only records THAT some callback arrived.

No live Telegram: the hook is captured through the same ``client.on`` stub
convention the other helper tests use, and the health timestamp is read back
through its real public getter.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from telethon import events

from backend.helper import client as helper_client


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _capture_callback_hook() -> dict:
    captured: dict = {}

    def _on(*args, **_kwargs):
        builder = args[0] if args else None

        def _decorator(func):
            if isinstance(builder, events.CallbackQuery):
                captured["builder"] = builder
                captured["hook"] = func
            return func

        return _decorator

    helper_client.register_helper_hooks(SimpleNamespace(on=_on))
    return captured


def _receipt_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.helper.client"
        and "[CALLBACK] received" in record.getMessage()
    ]


def test_receipt_records_the_callback_identity_and_coordinates(caplog):
    captured = _capture_callback_hook()
    assert isinstance(captured["builder"], events.CallbackQuery)

    event = SimpleNamespace(
        data=b"action:emoji_premium_inline",
        sender_id=7770001,
        chat_id=-1001234567890,
        message_id=4242,
        inline_message_id=None,
    )
    caplog.set_level(logging.INFO, logger="backend.helper.client")
    _run(captured["hook"](event))

    from backend.health import get_last_callback

    assert get_last_callback() > 0  # the health timestamp is still recorded

    lines = _receipt_lines(caplog)
    assert len(lines) == 1
    assert "data='action:emoji_premium_inline'" in lines[0]
    assert "sender_id=7770001" in lines[0]
    assert "chat_id=-1001234567890" in lines[0]
    assert "msg_id=4242" in lines[0]
    assert "inline_msg_id=''" in lines[0]


def test_receipt_is_bounded_and_never_raises_on_a_bare_event(caplog):
    hook = _capture_callback_hook()["hook"]
    caplog.set_level(logging.INFO, logger="backend.helper.client")

    _run(hook(SimpleNamespace(data=b"x" * 200, sender_id=1, chat_id=2, message_id=3)))
    line = _receipt_lines(caplog)[-1]
    assert "x" * 64 in line
    assert "x" * 65 not in line

    _run(hook(SimpleNamespace()))  # no data, no coordinates — still recorded, never raises
    lines = _receipt_lines(caplog)
    assert len(lines) == 2
    assert "data=''" in lines[-1]
    assert "sender_id=None" in lines[-1]
