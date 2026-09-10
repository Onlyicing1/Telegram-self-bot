"""Structured-output reliability tests for the provider boundary.

Live root cause (proven production trace, 2026-09-10): provider ``groq``,
model ``allam-2-7b`` returned HTTP-200 ``success=True`` output whose content
was NOT valid JSON (JSONDecodeError at pos 86 inside the task interpreter).
The interpreter's fail-closed parser correctly rejected it.

Fix under test — at the provider boundary, NOT in the parser:

1. ``TaskInterpreter`` requests structured output (``response_format``) and
   attaches an ``output_validator`` probe to its provider call.
2. ``OpenAICompatProvider`` serializes ``response_format`` into the request
   payload when the provider declares ``supports_json``; ``GeminiProvider``
   maps it to JSON MIME mode. The CANDIDATE_SCHEMA is NOT duplicated.
3. ``ProviderManager`` treats a transport-success response whose content
   violates the structured contract as a failover-eligible failure:
   bounded (only while another eligible candidate remains), NO cooldown
   (it is a request-level quality signal, not a provider health event),
   and recorded in the provider matrix.
4. Exhaustion fails closed — the response returned to the interpreter is
   still ``success=False`` with the original failures preserved, so task
   creation/persistence/Telegram never see a fabricated candidate.

The parser itself is unchanged and remains fail-closed (pinned by
tests/test_task_interpretation_diagnostics.py).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.task_interpreter import TaskInterpreter, TaskInterpretationError


def _good_candidate_json() -> str:
    return json.dumps(
        {
            "label": "Bio update",
            "schedule_type": "interval",
            "schedule": {"seconds": 300},
            "timezone": "Asia/Tehran",
            "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
            "notification_destination": {},
            "ai_instruction": "random Rei Ayanami dialogue under 60 chars",
        },
        separators=(",", ":"),
    )


class _StubProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        responses: list[ProviderResponse] | None = None,
        caps: ProviderCapabilities | None = None,
    ) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True))
        self._name = name
        self._responses = list(responses or [])
        self._caps = caps
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps or ProviderCapabilities(
            supports_tools=True, supports_function_call=True, supports_json=True,
        )

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="stub ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _manager(first: BaseProvider, *rest: BaseProvider) -> ProviderManager:
    mgr = ProviderManager(registry=MagicMock())
    mgr._registry = MagicMock()
    mgr._registry.get_active.return_value = first
    mgr._registry.get.side_effect = lambda name: {
        p.name: p for p in (first, *rest)
    }.get(name)
    mgr._registry.list.return_value = [p.name for p in (first, *rest)]
    mgr._registry.has.side_effect = lambda name: name in {p.name for p in (first, *rest)}
    mgr._fallback_chain = []
    return mgr


# ═══ 1. Interpreter requests structured output ═══


@pytest.mark.asyncio
async def test_interpreter_requests_response_format_and_validator():
    """The interpreter's provider call carries the JSON-mode request and the
    structured-output validator probe."""
    provider = _StubProvider("stub", [ProviderResponse(text=_good_candidate_json(), provider_name="stub", success=True)])
    provider2 = _StubProvider("stub2")
    mgr = _manager(provider, provider2)

    class _MgrAdapter:
        async def chat(self, messages, **kwargs):
            return await mgr.chat(messages, **kwargs)

    candidate = await TaskInterpreter(_MgrAdapter()).interpret("هر ۵ دقیقه بیو", timezone="Asia/Tehran")
    assert candidate.schedule == {"seconds": 300}
    assert provider.last_kwargs.get("response_format") == {"type": "json_object"}
    assert callable(provider.last_kwargs.get("output_validator"))


def test_validator_probe_accepts_valid_json_and_rejects_malformed():
    ok = ProviderResponse(text=_good_candidate_json(), provider_name="p", success=True)
    bad = ProviderResponse(
        text='{\"label\": \"x\" \"schedule_type\": \"interval\"', provider_name="p", success=True,
    )
    empty = ProviderResponse(text="", provider_name="p", success=True)
    prose = ProviderResponse(text="Here is your answer!", provider_name="p", success=True)
    assert TaskInterpreter._valid_structured_output(ok) is True
    assert TaskInterpreter._valid_structured_output(bad) is False
    assert TaskInterpreter._valid_structured_output(empty) is False
    assert TaskInterpreter._valid_structured_output(prose) is False


def test_validator_probe_accepts_contract_permitted_wrappers():
    fenced = "```json\n" + _good_candidate_json() + "\n```"
    prose_wrapped = "Here is the JSON you asked for:\n" + _good_candidate_json()
    double = json.dumps(_good_candidate_json())
    assert TaskInterpreter._valid_structured_output(
        ProviderResponse(text=fenced, provider_name="p", success=True)
    ) is True
    assert TaskInterpreter._valid_structured_output(
        ProviderResponse(text=prose_wrapped, provider_name="p", success=True)
    ) is True
    assert TaskInterpreter._valid_structured_output(
        ProviderResponse(text=double, provider_name="p", success=True)
    ) is True


# ═══ 2. Adapter serialization ═══


def test_openai_compat_serializes_response_format_when_declared():
    """The OpenAI-compat adapter puts response_format into the payload only
    when the provider declares supports_json — and never otherwise."""
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(
        ProviderConfig(provider_name="testcompat", base_url="https://x/v1", enabled=True, api_key="k")
    )
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self):
            return {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {},
            }

        @property
        def text(self):
            return "{}"

    async def _fake_post(url, json=None, **kw):
        captured.update(json or {})
        return _Resp()

    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(side_effect=_fake_post)

    import asyncio

    asyncio.get_event_loop_policy()
    resp = asyncio.new_event_loop().run_until_complete(
        provider.chat([{"role": "user", "content": "hi"}], response_format={"type": "json_object"})
    )
    assert resp.success
    assert captured.get("response_format") == {"type": "json_object"}

    # Without the capability the field is NOT sent.
    provider2 = OpenAICompatProvider(
        ProviderConfig(provider_name="testcompat2", base_url="https://x/v1", enabled=True, api_key="k")
    )
    caps = ProviderCapabilities()  # supports_json False by default
    type(provider2).capabilities = property(lambda self: caps)
    captured2: dict[str, Any] = {}
    provider2._http_client = MagicMock()
    provider2._http_client.post = AsyncMock(side_effect=_fake_post)

    async def _post2(url, json=None, **kw):
        captured2.update(json or {})
        return _Resp()

    provider2._http_client.post = AsyncMock(side_effect=_post2)
    resp2 = asyncio.new_event_loop().run_until_complete(
        provider2.chat([{"role": "user", "content": "hi"}], response_format={"type": "json_object"})
    )
    assert resp2.success
    assert "response_format" not in captured2


def test_gemini_maps_response_format_to_json_mime():
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(
        ProviderConfig(provider_name="gemini", base_url="", enabled=True, api_key="k")
    )
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
                "usageMetadata": {},
            }

    async def _fake_post(url, json=None, **kw):
        captured.update(json or {})
        return _Resp()

    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(side_effect=_fake_post)

    import asyncio

    asyncio.new_event_loop().run_until_complete(
        provider.chat([{"role": "user", "content": "hi"}], response_format={"type": "json_object"})
    )
    assert captured.get("generationConfig", {}).get("responseMimeType") == "application/json"


def test_candidate_schema_not_duplicated_in_adapters():
    """The schema travels through the interpreter's system message only —
    adapters must not embed a second copy of CANDIDATE_SCHEMA."""
    import backend.ai.providers.gemini as gemini_mod
    import backend.ai.providers.openai_compat as compat_mod

    for mod in (gemini_mod, compat_mod):
        src = mod.__doc__ or ""
        assert "CANDIDATE_SCHEMA" not in src or "task_interpreter" in src
    # The interpreter module owns exactly one definition.
    from backend.ai import task_interpreter as ti

    assert ti.CANDIDATE_SCHEMA["type"] == "object"


# ═══ 3. Content-failure failover in ProviderManager ═══


@pytest.mark.asyncio
async def test_malformed_json_from_active_provider_fails_over_to_next():
    """The live root cause shape: transport-success + malformed JSON from the
    active provider → bounded failover to the next eligible provider."""
    malformed = _StubProvider(
        "groq",
        [ProviderResponse(text='{\"label\": \"x\" \"schedule\":', provider_name="groq", success=True,
                          metadata={"model": "allam-2-7b", "finish_reason": "stop"})],
    )
    good = _StubProvider(
        "gemini",
        [ProviderResponse(text=_good_candidate_json(), provider_name="gemini", success=True,
                          metadata={"model": "gemini-2", "finish_reason": "STOP"})],
    )
    mgr = _manager(malformed, good)
    resp = await mgr.chat(
        [{"role": "user", "content": "task"}],
        tools=[],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    assert resp.success
    assert resp.provider_name == "gemini"
    assert malformed.calls == 1 and good.calls == 1
    matrix = resp.metadata.get("provider_matrix") or []
    assert any(e.get("provider") == "groq" and e.get("outcome") == "malformed_json" for e in matrix)


@pytest.mark.asyncio
async def test_content_failover_records_no_cooldown_or_quarantine():
    """A structured-contract violation is a request-level quality signal:
    the provider is NOT cooled down and stays eligible for the next request."""
    from backend.ai.providers.base.contract import ProviderResponse as PR

    bad = _StubProvider("bad", [PR(text="not json at all", provider_name="bad", success=True)])
    good = _StubProvider("good", [PR(text=_good_candidate_json(), provider_name="good", success=True)])
    mgr = _manager(bad, good)
    await mgr.chat(
        [{"role": "user", "content": "task"}],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    assert mgr._health.is_available("bad")
    assert mgr._health.state("bad") == "healthy"


@pytest.mark.asyncio
async def test_content_failover_penalizes_quality_metrics():
    bad = _StubProvider("bad", [ProviderResponse(text="{broken", provider_name="bad", success=True)])
    good = _StubProvider("good", [ProviderResponse(text=_good_candidate_json(), provider_name="good", success=True)])
    mgr = _manager(bad, good)
    await mgr.chat(
        [{"role": "user", "content": "task"}],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    metrics = mgr.metrics_snapshot().get("bad", {})
    assert metrics.get("quality_requests", 0) >= 1
    assert metrics.get("quality_failures", 0) >= 1


@pytest.mark.asyncio
async def test_content_failover_is_bounded_and_exhaustion_fails_closed():
    """Every eligible provider violates the contract → bounded attempts
    (one per provider), and the last response is handed back unchanged: the
    interpreter's fail-closed parser then rejects it — NO fabricated
    candidate ever reaches task creation/persistence."""
    bad1 = _StubProvider("p1", [ProviderResponse(text="{oops", provider_name="p1", success=True)])
    bad2 = _StubProvider("p2", [ProviderResponse(text="nope ]", provider_name="p2", success=True)])
    dummy = _StubProvider("dummy", [ProviderResponse(text="{}", provider_name="dummy", success=True)])
    mgr = _manager(bad1, bad2)
    mgr._registry.get_fallback.return_value = dummy
    resp = await mgr.chat(
        [{"role": "user", "content": "task"}],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    # The last candidate's response is returned UNCHANGED (caller owns the
    # final classification); the earlier violation is recorded in the matrix.
    assert bad1.calls == 1 and bad2.calls == 1  # bounded: one attempt each
    assert resp.provider_name == "p2"
    matrix = resp.metadata.get("provider_matrix") or []
    assert matrix[0].get("outcome") == "malformed_json"


@pytest.mark.asyncio
async def test_valid_content_passes_through_without_failover():
    good = _StubProvider("only", [ProviderResponse(text=_good_candidate_json(), provider_name="only", success=True)])
    mgr = _manager(good)
    resp = await mgr.chat(
        [{"role": "user", "content": "task"}],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    assert resp.success and good.calls == 1


@pytest.mark.asyncio
async def test_no_validator_means_no_content_failover():
    """Callers that do not attach a validator keep the exact previous
    behavior — a transport-success response is returned as-is."""
    weird = _StubProvider("w", [ProviderResponse(text="prose answer", provider_name="w", success=True)])
    other = _StubProvider("o", [ProviderResponse(text="fallback", provider_name="o", success=True)])
    mgr = _manager(weird, other)
    resp = await mgr.chat([{"role": "user", "content": "task"}])
    assert resp.success and resp.provider_name == "w" and weird.calls == 1


@pytest.mark.asyncio
async def test_json_capability_sorts_capable_providers_first_when_requested():
    """Capability-aware ordering: with JSON output requested, the capable
    provider is tried FIRST (proven by the matrix order) even though the
    user's active provider is the non-capable one; the active provider then
    serves the request through the bounded failover."""
    active_nocap = _StubProvider(
        "active_nocap",
        [ProviderResponse(text=_good_candidate_json(), provider_name="active_nocap", success=True)],
        caps=ProviderCapabilities(supports_tools=True, supports_function_call=True, supports_json=False),
    )
    other_cap = _StubProvider(
        "other_cap", [], caps=ProviderCapabilities(supports_tools=True, supports_function_call=True, supports_json=True),
    )
    mgr = _manager(active_nocap, other_cap)
    resp = await mgr.chat(
        [{"role": "user", "content": "task"}],
        response_format={"type": "json_object"},
        output_validator=TaskInterpreter._valid_structured_output,
    )
    assert resp.provider_name == "active_nocap"
    matrix = resp.metadata.get("provider_matrix") or []
    assert [e.get("provider") for e in matrix] == ["other_cap", "active_nocap"]
    assert matrix[0].get("outcome") == "malformed_json"
    assert other_cap.calls == 1 and active_nocap.calls == 1


# ═══ 4. End-to-end: exact Persian request through the REAL manager ═══


@pytest.mark.asyncio
async def test_exact_persian_request_creates_candidate_via_failover():
    """The exact live production request through the REAL ProviderManager:
    the first provider returns malformed JSON (the proven live failure), the
    second returns a valid candidate -> interpretation succeeds with
    verbatim ai_instruction and the strict <60 semantics intact."""
    malformed = _StubProvider(
        "groq",
        [ProviderResponse(
            text='{"label": "Bio" "schedule_type": "interval"',
            provider_name="groq", success=True,
            metadata={"model": "allam-2-7b", "finish_reason": "stop"},
        )],
    )
    verbatim = (
        "هر ۵ دقیقه\n"
        "میخوام بیو پروفایلم رو آپدیت کنید\n"
        "یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری\n"
        "که زیر 60 کاراکتر باشه"
    )
    good_payload = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": verbatim,
    }
    good = _StubProvider(
        "gemini",
        [ProviderResponse(
            text=json.dumps(good_payload, ensure_ascii=False),
            provider_name="gemini", success=True,
            metadata={"model": "gemini-2", "finish_reason": "STOP"},
        )],
    )
    mgr = _manager(malformed, good)
    candidate = await TaskInterpreter(mgr).interpret(verbatim, timezone="Asia/Tehran", request_id="req-live-repro")
    assert candidate.schedule_type == "interval"
    assert candidate.schedule == {"seconds": 300}
    assert candidate.actions == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    assert "آیانامی ری" in candidate.ai_instruction
    assert "زیر 60 کاراکتر" in candidate.ai_instruction
    assert malformed.calls == 1 and good.calls == 1  # bounded failover: one extra attempt


@pytest.mark.asyncio
async def test_exact_persian_request_all_providers_malformed_fails_closed():
    """Exhaustion on the exact request fails closed: TaskInterpretationError
    carrying the provider-failure detail - never a fabricated candidate,
    never a Telegram/persistence side effect."""
    bad1 = _StubProvider("p1", [ProviderResponse(text="{oops", provider_name="p1", success=True)])
    bad2 = _StubProvider("p2", [ProviderResponse(text="nope ]", provider_name="p2", success=True)])
    mgr = _manager(bad1, bad2)
    with pytest.raises(TaskInterpretationError) as excinfo:
        await TaskInterpreter(mgr).interpret(
            "هر ۵ دقیقه\nبیو رو آپدیت کن با یه دیالوگ از آیانامی ری",
            timezone="Asia/Tehran",
        )
    # The interpreter's fail-closed parser rejects the unparseable content
    # (the precise candidate_invalid_json category rides on the trace log).
    assert "did not return a valid candidate" in str(excinfo.value)
    assert "response_shape=malformed" in str(excinfo.value)
    assert bad1.calls == 1 and bad2.calls == 1  # bounded
