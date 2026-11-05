"""\"All Results\" (``action:ai_test_details``) — real callback-path tests.

The renderer tests prove the button exists and the diagnostics body renders.
They never prove a real Telegram click reaches the handler and changes the
panel, which is how the button shipped dead: a real run's diagnostics are far
longer than Telegram's 4096-character message limit, the edit was rejected, and
the click ended acknowledged with nothing on screen.

These tests drive the PRODUCTION router (``register_callback_handlers``) with
real Telethon callback events over fakes for the Telegram RPCs, so admission,
acknowledgement, dispatch, registry resolution, handler invocation and the
panel edit are all exercised end to end. Nothing touches the network.
"""
from __future__ import annotations

import itertools
import time

import pytest
from telethon import events, types

from backend.helper import callback_gate
from backend.helper import panels as panels_module
from backend.helper.lifecycle import get_lifecycle

OWNER = 8111
MAX_MESSAGE_CHARS = 4096  # Telegram's per-message text limit

_QUERY_IDS = itertools.count(90001)
_MSG_IDS = itertools.count(5000)


# ── fakes for the network boundary ──


class _CallbackClient:
    def __init__(self):
        self.handlers: dict = {}

    def on(self, builder):
        def decorator(func):
            self.handlers[type(builder).__name__] = func
            return func

        return decorator


class _PanelMessage:
    """Fake panel message: enforces the real message-length limit."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.buttons: list = []
        self.answers: list[float] = []
        self.edits_at: list[float] = []

    async def edit(self, text: str = "", buttons=None, **kwargs) -> None:
        if len(text) > MAX_MESSAGE_CHARS:
            raise RuntimeError("Message is too long")
        self.texts.append(text)
        self.buttons.append(buttons or [])
        self.edits_at.append(time.monotonic())

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append(time.monotonic())

    @property
    def text(self) -> str:
        return self.texts[-1] if self.texts else ""


def _flatten(buttons) -> list[str]:
    datas = []
    for row in buttons or []:
        for btn in row or []:
            data = getattr(btn, "data", b"")
            datas.append(data.decode() if isinstance(data, bytes) else str(data))
    return datas


def _wired_router():
    """Production wiring: AI actions registered, callback router attached."""
    from backend.bot.handlers import ai as ai_module

    client = _CallbackClient()
    ai_module.register(client, OWNER)
    panels_module.register_callback_handlers(client, OWNER)
    return ai_module, client.handlers["CallbackQuery"]


def _click(data: bytes, msg_id: int):
    event = events.CallbackQuery.build(
        types.UpdateBotCallbackQuery(
            query_id=next(_QUERY_IDS),
            user_id=OWNER,
            peer=types.PeerUser(OWNER),
            msg_id=msg_id,
            chat_instance=5,
            data=data,
        )
    )
    message = _PanelMessage()
    event.edit = message.edit
    event.answer = message.answer
    return event, message


async def _dispatch(router, data: bytes, *, with_session: bool = True) -> _PanelMessage:
    msg_id = next(_MSG_IDS)
    event, message = _click(data, msg_id)
    sessions = get_lifecycle().sessions
    if with_session:
        sessions.create(OWNER, msg_id, panel_type="menu")
    try:
        await router(event)
    finally:
        if with_session:
            sessions.destroy(OWNER, msg_id)
    return message


@pytest.fixture(autouse=True)
def _clean_gate():
    callback_gate.reset()
    yield
    callback_gate.reset()


@pytest.fixture(autouse=True)
def _restore_payload():
    from backend.bot.handlers import ai as ai_module

    saved = ai_module._last_test_payload
    yield
    ai_module._last_test_payload = saved


# ── payloads ──


def _results(n_results: int, *, every_fails: int = 3) -> list[dict]:
    providers = ["groq", "openai", "openrouter", "cerebras", "mistral"]
    results = []
    for i in range(n_results):
        provider = providers[i % len(providers)]
        failed = i % every_fails == 0
        results.append({
            "provider": provider,
            "display_name": provider.title(),
            "icon": "◇",
            "model": f"{provider}/some-quite-long-model-identifier-{i}",
            "status": "INVALID_MODEL" if failed else "AVAILABLE",
            "latency_s": 0.42,
            "http_status": 404 if failed else 200,
            "retry_after": 17 if i == 3 else None,
            "error": ("models/x is not found for API version v1beta") if failed else None,
            "error_type": "model_not_found" if failed else None,
            "provider_code": "404" if failed else None,
            "provider_error_type": "NOT_FOUND" if failed else None,
            "provider_error_message": (
                "models/x is not found for API version v1beta"
            ) if failed else None,
            "failure_origin": "model_lookup" if failed else None,
            "operation": "generateContent",
            "api_version": "v1beta",
            "endpoint": f"models/{provider}-model-{i}:generateContent",
        })
    return results


def _payload(n_results: int = 4) -> dict:
    results = _results(n_results)
    return {
        "success": True,
        "results": results,
        "summary": {
            "available": sum(1 for r in results if r["status"] == "AVAILABLE"),
            "failed": sum(1 for r in results if r["status"] != "AVAILABLE"),
            "rate_limited": 0,
            "not_configured": 0,
            "invalid": sum(1 for r in results if r["status"] == "INVALID_MODEL"),
            "insufficient_credits": 0,
        },
    }


# ── 1. the rendered button carries the expected callback data ──


def test_the_results_view_renders_the_all_results_callback():
    from backend.bot.handlers import ai as ai_module

    _, buttons = ai_module._render_test_results(_payload())
    assert "action:ai_test_details" in _flatten(buttons)


# ── 2 + 3. registry resolution and router dispatch ──


def test_the_action_registry_resolves_the_details_handler():
    from backend.bot.handlers import ai as ai_module

    _wired_router()
    assert panels_module.get_action("ai_test_details") is ai_module._ai_test_details_action


@pytest.mark.asyncio
async def test_a_real_callback_dispatches_to_the_details_handler(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload()
    _, router = _wired_router()

    calls: list[dict] = []
    real_handler = ai_module._ai_test_details_action

    async def spy(event, extra, chat_id):
        calls.append({"extra": extra, "chat_id": chat_id})
        return await real_handler(event, extra, chat_id)

    monkeypatch.setitem(panels_module._actions, "ai_test_details", spy)
    await _dispatch(router, b"action:ai_test_details")

    assert len(calls) == 1
    assert calls[0]["extra"] == ""


# ── 4 + 5 + 6. handler runs, panel is edited, click acknowledged first ──


@pytest.mark.asyncio
async def test_the_diagnostics_render_through_the_production_panel_path():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload()
    _, router = _wired_router()

    message = await _dispatch(router, b"action:ai_test_details")

    assert message.texts, "the tap must edit the panel — otherwise it looks dead"
    assert "All Results" in message.text
    assert "HTTP 404" in message.text
    assert "INVALID_MODEL" in message.text
    assert "NOT_FOUND" in message.text
    assert "model_lookup" in message.text
    assert "is not found" in message.text
    assert "retry-after: 17s" in message.text
    datas = _flatten(message.buttons[-1])
    assert "action:ai_test_models" in datas
    assert "panel:ai_model" in datas


@pytest.mark.asyncio
async def test_the_click_is_acknowledged_before_the_panel_is_painted():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload()
    _, router = _wired_router()

    message = await _dispatch(router, b"action:ai_test_details")

    assert message.answers, "every click must be acknowledged"
    assert message.answers[0] <= message.edits_at[0], (
        "acknowledgement must not wait for the diagnostic render"
    )


# ── 7. a full run must page instead of failing the edit ──


@pytest.mark.asyncio
async def test_a_full_run_pages_instead_of_failing_the_edit():
    from backend.bot.handlers import ai as ai_module

    payload = _payload(60)
    ai_module._last_test_payload = payload

    # The unpaged body is what used to be sent, and it cannot fit one message.
    blocks = ai_module._test_detail_blocks(payload["results"])
    unpaged = "\n\n".join(blocks)
    assert len(unpaged) > MAX_MESSAGE_CHARS

    _, router = _wired_router()
    # _PanelMessage raises above 4096 chars exactly like Telegram, so a single
    # unbounded body would produce ZERO edits here.
    message = await _dispatch(router, b"action:ai_test_details")

    assert len(message.texts) == 1
    assert len(message.text) <= MAX_MESSAGE_CHARS
    assert "Page 1/" in message.text
    datas = _flatten(message.buttons[-1])
    assert "action:ai_test_details:1" in datas
    # Provider attribution survives paging.
    assert any(name in message.text for name in ("Groq", "Openai", "Mistral"))


@pytest.mark.asyncio
async def test_paging_covers_every_result_exactly_once():
    from backend.bot.handlers import ai as ai_module
    from backend.helper.panel_render import render_edit

    payload = _payload(60)
    ai_module._last_test_payload = payload
    models = [r["model"] for r in payload["results"]]

    seen: list[str] = []
    page = 0
    while True:
        title, body, buttons = await ai_module._ai_test_details_action(None, str(page), 0)
        rendered, _ = render_edit(title, body, buttons)
        assert len(rendered) <= MAX_MESSAGE_CHARS
        seen.extend(model for model in models if f"`{model}`" in body)
        datas = _flatten(buttons)
        if f"action:ai_test_details:{page + 1}" not in datas:
            break
        page += 1

    assert page >= 1
    assert sorted(seen) == sorted(models)
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_details_pages_stay_on_the_registered_action_id():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload(60)
    _, _, buttons = await ai_module._ai_test_details_action(None, "0", 0)

    assert "action:ai_test_details:1" in _flatten(buttons)


# ── 8. missing / expired payloads explain themselves instead of silence ──


@pytest.mark.asyncio
async def test_a_missing_payload_explains_itself_through_the_router():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = {}
    _, router = _wired_router()

    message = await _dispatch(router, b"action:ai_test_details")

    assert message.texts
    assert "No results yet" in message.text
    assert "action:ai_test_models" in _flatten(message.buttons[-1])


@pytest.mark.asyncio
async def test_an_expired_payload_without_results_explains_itself():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = {"success": False, "results": [], "summary": {}}
    _, router = _wired_router()

    message = await _dispatch(router, b"action:ai_test_details")

    assert "No results yet" in message.text
    assert "action:ai_test_models" in _flatten(message.buttons[-1])


# ── 9. a failing handler is visible, never a silent no-op ──


@pytest.mark.asyncio
async def test_a_failing_action_shows_a_failure_instead_of_doing_nothing(monkeypatch):
    _, router = _wired_router()

    async def boom(event, extra, chat_id):
        raise RuntimeError("internal detail that must not be shown")

    monkeypatch.setitem(panels_module._actions, "action_that_fails", boom)
    message = await _dispatch(router, b"action:action_that_fails")

    assert message.texts, "a failing action must still respond visibly"
    assert "failed" in message.text.lower()
    assert "RuntimeError" in message.text
    assert "internal detail" not in message.text


# ── 10. unauthorized clicks stay rejected ──


@pytest.mark.asyncio
async def test_unauthorized_callback_is_rejected_without_editing(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload()
    _, router = _wired_router()

    monkeypatch.setattr(panels_module, "is_owner", lambda event, owner_id: False)
    message = await _dispatch(router, b"action:ai_test_details")

    assert message.texts == [], "a non-owner tap must not render diagnostics"
    assert message.answers


@pytest.mark.asyncio
async def test_a_tap_without_a_panel_session_is_dropped():
    from backend.bot.handlers import ai as ai_module

    ai_module._last_test_payload = _payload()
    _, router = _wired_router()

    message = await _dispatch(router, b"action:ai_test_details", with_session=False)

    assert message.texts == []
    assert message.answers
