"""
GeminiProvider — Google Gemini adapter (real implementation).

Uses the Gemini REST API via httpx async. Supports text generation
with the OpenAI-compatible endpoint format.
"""
from __future__ import annotations

import asyncio
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
    """Google Gemini provider via REST API."""

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
            supports_streaming=True,
            supports_images=True,
            supports_reasoning=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
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
            else:
                gemini_role = "user" if role == "user" else "model"
                contents.append({
                    "role": gemini_role,
                    "parts": [{"text": content}],
                })

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
                    return ProviderResponse(text=f"Rate limited.", provider_name=self.name, success=False)

                if resp.status_code >= 400:
                    error_msg = resp.text[:200]
                    try:
                        error_data = resp.json()
                        error_msg = error_data.get("error", {}).get("message", error_msg)
                    except Exception:
                        pass
                    logger.warning("Gemini API error %d: %s", resp.status_code, error_msg)
                    return ProviderResponse(text=f"API error ({resp.status_code}): {error_msg}", provider_name=self.name, success=False)

                data = resp.json()
                candidates = data.get("candidates", [])
                text = ""
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = " ".join(p.get("text", "") for p in parts)
                usage = data.get("usageMetadata", {})

                return ProviderResponse(
                    text=text,
                    provider_name=self.name,
                    success=True,
                    usage={
                        "prompt_tokens": usage.get("promptTokenCount", 0),
                        "completion_tokens": usage.get("candidatesTokenCount", 0),
                    },
                    metadata={"latency": latency, "model": model},
                )

            except httpx.TimeoutException:
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(text=f"Timeout after {self._config.timeout}s.", provider_name=self.name, success=False)
            except Exception as exc:
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(text=f"Error: {exc}", provider_name=self.name, success=False)

        return ProviderResponse(text="Failed after retries.", provider_name=self.name, success=False)

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)
