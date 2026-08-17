"""
GeminiProvider — Google Gemini adapter (real implementation).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(BaseProvider):
    PROVIDER_NAME = "gemini"
    PROVIDER_VERSION = "1.0.0"

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

        model = kwargs.get("model", self._config.default_model)
        url = f"{_GEMINI_BASE}/models/{model}:generateContent?key={self._config.api_key}"

        system_text = ""
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_text = content
            elif role == "tool":
                tool_name = msg.get("name", "")
                try:
                    result_data = json.loads(content) if isinstance(content, str) else content
                except (json.JSONDecodeError, TypeError):
                    result_data = {"raw": content}
                contents.append({
                    "role": "model",
                    "parts": [{"functionResponse": {"name": tool_name, "response": {"name": tool_name, "content": result_data}}}],
                })
            elif role == "assistant" and msg.get("tool_calls"):
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": content})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
                contents.append({"role": "model", "parts": parts})
            else:
                gemini_role = "user" if role == "user" else "model"
                contents.append({"role": gemini_role, "parts": [{"text": content}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": kwargs.get("temperature", self._config.temperature),
                "maxOutputTokens": kwargs.get("max_tokens", self._config.max_tokens),
            },
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}

        for attempt in range(self._config.retry_count + 1):
            try:
                t0 = time.perf_counter()
                resp = await self._http_client.post(url, json=payload)
                latency = time.perf_counter() - t0

                if resp.status_code == 429:
                    retry_after = 5
                    logger.warning("Gemini rate limited, waiting %ds", retry_after)
                    if attempt < self._config.retry_count:
                        await asyncio.sleep(retry_after)
                        continue
                    return ProviderResponse(text="Rate limited.", provider_name=self.name, success=False, metadata={"http_status": 429, "retry_after": retry_after})

                if resp.status_code >= 400:
                    error_msg = resp.text[:200]
                    provider_error_code = ""
                    provider_error_type = ""
                    try:
                        error_data = resp.json()
                        err_obj = error_data.get("error", {})
                        error_msg = err_obj.get("message", error_msg)
                        provider_error_code = str(err_obj.get("code", ""))
                        provider_error_type = err_obj.get("status", "")
                    except Exception:
                        pass
                    logger.warning("Gemini API error %d: %s", resp.status_code, error_msg)
                    return ProviderResponse(text=f"API error ({resp.status_code}): {error_msg}", provider_name=self.name, success=False, metadata={"http_status": resp.status_code, "provider_error_code": provider_error_code, "provider_error_type": provider_error_type})

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
                            tool_calls.append({"id": fc.get("id", ""), "name": fc.get("name", ""), "arguments": fc.get("args", {})})
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
                    metadata={"latency": latency, "model": model, "finish_reason": finish_reason},
                )

            except httpx.TimeoutException:
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(text=f"Timeout after {self._config.timeout}s.", provider_name=self.name, success=False, metadata={"error_type": "timeout"})
            except Exception as exc:
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(text=f"Error: {exc}", provider_name=self.name, success=False, metadata={"error_type": type(exc).__name__})

        return ProviderResponse(text="Failed after retries.", provider_name=self.name, success=False, metadata={"error_type": "retry_exhausted"})

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
