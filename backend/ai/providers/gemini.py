"""
GeminiProvider — Google Gemini adapter (real implementation).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.redaction import redact_secrets

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_VERSION = _GEMINI_BASE.rsplit("/", 1)[-1]
_OPERATION = "generateContent"
_MAX_ERROR_MESSAGE = 300


def _bounded_text(value: Any, limit: int = _MAX_ERROR_MESSAGE) -> str:
    clean = " ".join(redact_secrets(str(value or "")).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _gemini_diagnostics(model: str) -> dict[str, str]:
    """Safe request identity: no base URL, no query, never the API key."""
    return {
        "model": model,
        "operation": _OPERATION,
        "api_version": _GEMINI_VERSION,
        "endpoint": f"models/{model}:{_OPERATION}",
    }


def _retry_after_seconds(headers: Any, default: int = 5) -> int:
    try:
        raw = str(headers.get("retry-after") or "").strip()
    except Exception:
        return default
    if raw.isdigit():
        return max(0, min(int(raw), 3600))
    return default


def _error_fields(resp: Any) -> tuple[str, str, str, list[str]]:
    """Google's structured error, bounded — never the raw response body.

    Returns ``(code, status, message, reasons)``. The message is sanitized
    and bounded here, so a credential echoed into an error body is removed at
    the source, before any log line or diagnostic payload can carry it.
    """
    raw_text = ""
    try:
        raw_text = resp.text or ""
    except Exception:
        raw_text = ""
    message = _bounded_text(raw_text)
    code = ""
    provider_status = ""
    reasons: list[str] = []
    try:
        data = resp.json()
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            if err.get("message"):
                message = _bounded_text(err.get("message"))
            code = str(err.get("code") or "")
            provider_status = str(err.get("status") or "")
            for item in err.get("details") or []:
                if isinstance(item, dict):
                    reason = str(item.get("reason") or "").strip()
                    if reason and reason not in reasons:
                        reasons.append(_bounded_text(reason, 80))
                if len(reasons) >= 5:
                    break
    except Exception:
        pass
    return code, provider_status, message, reasons


def _failure_origin(status_code: int, provider_status: str, message: str) -> str:
    """Where the failure actually came from, from Google's own signals."""
    state = (provider_status or "").upper()
    text = (message or "").lower()
    if status_code in (401, 403) or state in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return "authentication"
    if status_code == 429 or state == "RESOURCE_EXHAUSTED":
        return "rate_limit"
    if status_code == 404:
        return "model_lookup" if ("model" in text or "models/" in text) else "endpoint"
    if status_code == 400:
        return "request_validation"
    if status_code >= 500 or state == "INTERNAL":
        return "server"
    return "provider"


_FAILURE_TYPES = {
    "authentication": "auth",
    "model_lookup": "model_not_found",
    "rate_limit": "rate_limited",
    "request_validation": "request",
    "server": "server",
}


def _to_gemini_schema(value: Any) -> Any:
    """Recursively convert schema ``type`` values to Gemini's Type spelling.

    Gemini's function-declaration schema uses its own enum spelling (STRING /
    INTEGER / NUMBER / BOOLEAN / ARRAY / OBJECT, ...). The conversion must
    cover nested ``properties`` and ``items`` too — uppercasing only the top
    level left array/nested schemas in lowercase, so a correct nested schema
    could be rejected or ignored. Everything else (required, enum, minimum,
    maximum, description, default, items) is preserved verbatim.
    """
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "type" and isinstance(item, str):
                converted[key] = item.upper()
            else:
                converted[key] = _to_gemini_schema(item)
        return converted
    if isinstance(value, list):
        return [_to_gemini_schema(item) for item in value]
    return value


class GeminiProvider(BaseProvider):
    PROVIDER_NAME = "gemini"
    PROVIDER_VERSION = "1.0.0"
    #: Gemini validates the current tool turn: the model's own opaque thought
    #: signature must come back in the exact functionCall part. A synthetic
    #: conversation can never carry one, so callers that build their own calls
    #: (the continuation diagnostic) must not send them here.
    REQUIRES_NATIVE_TOOL_SIGNATURE = True

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("gemini")
        super().__init__(config)
        self._http_client: httpx.AsyncClient | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True, supports_images=True, supports_reasoning=True,
            supports_tools=True, supports_json=True, supports_function_call=True, supports_long_context=True,
        )

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        self._http_client = None

    def health(self) -> dict[str, Any]:
        if not self._config.api_key:
            return {"healthy": False, "provider": self.name, "reason": "no API key"}
        if not self._config.enabled:
            return {"healthy": False, "provider": self.name, "reason": "disabled"}
        return {"healthy": True, "provider": self.name, "version": self.PROVIDER_VERSION, "enabled": True}

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        if not self._config.api_key or not self._config.enabled:
            return self._disabled_response()

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._config.timeout)

        # The selected id is sent VERBATIM: the request must be for exactly the
        # model the owner chose. A retired id fails as that id (this provider's
        # own 404, attributed to it) — never as a substituted model.
        model = str(kwargs.get("model") or self._config.default_model or "").strip()
        url = f"{_GEMINI_BASE}/models/{model}:generateContent?key={self._config.api_key}"

        # Gemini takes ONE systemInstruction; the dispatcher may legitimately
        # send several system messages (merged rules, runtime context,
        # conversation state, tool contract). Every section must be preserved
        # in order — assigning per message kept only the last one, silently
        # dropping the rules/output contract for every Gemini-routed request.
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        # Gemini's contract puts function responses in a USER turn, and every
        # response answering one function-call turn belongs to the SAME
        # Content, in call order. The previous mapping emitted one ``model``
        # turn per response, which Gemini does not accept: a two-call turn
        # became model(functionCall, functionCall) -> model(functionResponse)
        # -> model(functionResponse), so the conversation could not continue.
        # Responses are accumulated here and flushed as ONE user Content
        # immediately before the next non-response turn.
        pending_responses: list[dict[str, Any]] = []

        def _flush_tool_responses() -> None:
            if pending_responses:
                contents.append({"role": "user", "parts": list(pending_responses)})
                pending_responses.clear()

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "tool":
                tool_name = str(msg.get("name", "") or "")
                try:
                    result_data = json.loads(content) if isinstance(content, str) else content
                except (json.JSONDecodeError, TypeError):
                    result_data = {"raw": content}
                function_response: dict[str, Any] = {
                    "name": tool_name,
                    "response": {"name": tool_name, "content": result_data},
                }
                call_id = str(msg.get("tool_call_id", "") or "").strip()
                if call_id:
                    # Echo the id of the call this response answers, so a
                    # multi-call turn's responses stay matched to their calls.
                    function_response["id"] = call_id
                pending_responses.append({"functionResponse": function_response})
                continue
            _flush_tool_responses()
            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append(content)
            elif role == "assistant" and msg.get("tool_calls"):
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": content})
                for index, tc in enumerate(msg["tool_calls"]):
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    function_call: dict[str, Any] = {
                        "name": fn.get("name", ""), "args": args,
                    }
                    call_id = str(tc.get("id", "") or "").strip()
                    if call_id:
                        # Gemini echoes this id back on the function call, so
                        # the response can be matched to the exact call.
                        function_call["id"] = call_id
                    part: dict[str, Any] = {"functionCall": function_call}
                    signature = tc.get("thought_signature")
                    if isinstance(signature, str) and signature:
                        part["thoughtSignature"] = signature
                    elif index == 0:
                        # Never invent one. Only the FIRST call of a turn must
                        # carry a signature (parallel calls legitimately have
                        # none), so a first part without one is the real defect.
                        logger.warning(
                            "GEMINI_THOUGHT_SIGNATURE_MISSING calls=%d "
                            "reason=provider_returned_no_signature",
                            len(msg["tool_calls"]),
                        )
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            else:
                gemini_role = "user" if role == "user" else "model"
                contents.append({"role": gemini_role, "parts": [{"text": content}]})
        _flush_tool_responses()

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": kwargs.get("temperature", self._config.temperature),
                "maxOutputTokens": kwargs.get("max_tokens", self._config.max_tokens),
            },
        }
        # Structured output: Gemini's JSON-MIME mode constrains generation to
        # valid JSON (no code fences, no prose). Serialized only when the
        # caller requests it and the capability is declared.
        if kwargs.get("response_format") and self.capabilities.supports_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        system_text = "\n\n".join(system_parts)
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}

        tools = kwargs.get("tools")
        if tools:
            declarations: list[dict[str, Any]] = []
            for tool_def in tools:
                fn = tool_def.get("function", {}) if isinstance(tool_def, dict) else {}
                params = fn.get("parameters") or {}
                decl: dict[str, Any] = {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                }
                if isinstance(params, dict) and params:
                    decl["parameters"] = _to_gemini_schema(params)
                declarations.append(decl)
            payload["tools"] = [{"functionDeclarations": declarations}]

        # Single attempt — retries, cooldown, and fallback are owned by the
        # ProviderManager so a rate-limited provider is never retried in a loop.
        try:
            t0 = time.perf_counter()
            resp = await self._http_client.post(url, json=payload)
            latency = time.perf_counter() - t0

            if resp.status_code == 429:
                code, provider_status, message, reasons = _error_fields(resp)
                retry_after = _retry_after_seconds(resp.headers)
                logger.warning(
                    "Gemini rate limited (retry_after=%ds code=%s status=%s)",
                    retry_after, code or "-", provider_status or "-",
                )
                return ProviderResponse(
                    text=f"Rate limited. {message}".strip(),
                    provider_name=self.name,
                    success=False,
                    metadata={
                        **_gemini_diagnostics(model),
                        "http_status": 429,
                        "retry_after": retry_after,
                        "provider_error_code": code,
                        "provider_error_type": provider_status,
                        "provider_error_message": message,
                        "provider_error_reasons": reasons,
                        "failure_type": "rate_limited",
                        "failure_origin": "rate_limit",
                    },
                )

            if resp.status_code >= 400:
                code, provider_status, message, reasons = _error_fields(resp)
                origin = _failure_origin(resp.status_code, provider_status, message)
                logger.warning(
                    "Gemini API error %d (%s/%s, origin=%s): %s",
                    resp.status_code, code or "-", provider_status or "-", origin, message,
                )
                return ProviderResponse(
                    text=f"API error ({resp.status_code}): {message}",
                    provider_name=self.name,
                    success=False,
                    metadata={
                        **_gemini_diagnostics(model),
                        "http_status": resp.status_code,
                        "provider_error_code": code,
                        "provider_error_type": provider_status,
                        "provider_error_message": message,
                        "provider_error_reasons": reasons,
                        "failure_type": _FAILURE_TYPES.get(origin, "provider"),
                        "failure_origin": origin,
                    },
                )

            data = resp.json()
            candidates = data.get("candidates", [])
            text = ""
            finish_reason = ""
            tool_calls: list[dict[str, Any]] = []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts: list[str] = []
                for p in parts:
                    if "text" in p:
                        text_parts.append(p["text"])
                    elif "functionCall" in p:
                        fc = p["functionCall"]
                        call: dict[str, Any] = {
                            "id": fc.get("id", ""),
                            "name": fc.get("name", ""),
                            "arguments": fc.get("args", {}),
                        }
                        signature = p.get("thoughtSignature")
                        if isinstance(signature, str) and signature:
                            # Opaque reasoning state; Google requires it back in
                            # THIS part, so it rides with its own call.
                            call["thought_signature"] = signature
                        tool_calls.append(call)
                text = " ".join(text_parts)
                finish_reason = candidates[0].get("finishReason", "")
            usage = data.get("usageMetadata", {})

            if not text and not tool_calls and finish_reason:
                if finish_reason == "MAX_TOKENS":
                    text = "Response truncated due to token limit."
                elif finish_reason == "SAFETY":
                    text = "Response blocked by content filter."
                elif finish_reason == "RECITATION":
                    text = "Response blocked due to recitation."

            return ProviderResponse(
                text=text, provider_name=self.name, success=True, tool_calls=tool_calls,
                usage={"prompt_tokens": usage.get("promptTokenCount", 0), "completion_tokens": usage.get("candidatesTokenCount", 0), "total_tokens": usage.get("totalTokenCount", 0)},
                metadata={"latency": latency, **_gemini_diagnostics(model), "finish_reason": finish_reason},
            )

        except httpx.TimeoutException:
            return ProviderResponse(
                text=f"Timeout after {self._config.timeout}s.",
                provider_name=self.name,
                success=False,
                metadata={**_gemini_diagnostics(model), "failure_type": "timeout", "failure_origin": "timeout"},
            )
        except Exception as exc:
            return ProviderResponse(
                text=f"Error: {_bounded_text(exc)}",
                provider_name=self.name,
                success=False,
                metadata={
                    **_gemini_diagnostics(model),
                    "failure_type": "network",
                    "failure_origin": "network",
                    "error": f"{type(exc).__name__}: {_bounded_text(exc)}",
                },
            )

    async def list_models(self) -> list[dict[str, Any]]:
        if not self._config.api_key or not self._config.enabled:
            return []
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._config.timeout)
        url = f"{_GEMINI_BASE}/models?key={self._config.api_key}&pageSize=100"
        try:
            resp = await self._http_client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("models", [])
        except Exception:
            return []

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)
