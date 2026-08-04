"""
OpenAI-compatible async provider base.

All providers that follow the OpenAI chat completions API format
inherit from this class. Handles async HTTP, retry, rate limits, timeouts.
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


class OpenAICompatProvider(BaseProvider):
    """Async base for OpenAI-compatible API providers."""

    PROVIDER_NAME = "openai_compat"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default(self.PROVIDER_NAME)
        super().__init__(config)
        self._http_client: httpx.AsyncClient | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_images=True,
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
        return {
            "healthy": True,
            "provider": self.name,
            "version": self.PROVIDER_VERSION,
            "enabled": True,
        }

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        if not self._config.api_key or not self._config.enabled:
            return self._disabled_response()

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._config.timeout,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
            )

        url = f"{self._config.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": kwargs.get("model", self._config.default_model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self._config.temperature),
            "max_tokens": kwargs.get("max_tokens", self._config.max_tokens),
        }
        if "top_p" in kwargs:
            payload["top_p"] = kwargs["top_p"]

        for attempt in range(self._config.retry_count + 1):
            try:
                t0 = time.perf_counter()
                resp = await self._http_client.post(url, json=payload)
                latency = time.perf_counter() - t0

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("retry-after", "5"))
                    logger.warning("%s rate limited, waiting %ds", self.name, retry_after)
                    if attempt < self._config.retry_count:
                        await asyncio.sleep(retry_after)
                        continue
                    return ProviderResponse(
                        text=f"Rate limited. Try again in {retry_after}s.",
                        provider_name=self.name,
                        success=False,
                    )

                if resp.status_code >= 400:
                    error_msg = "Unknown error"
                    try:
                        error_data = resp.json()
                        error_msg = error_data.get("error", {}).get("message", error_msg)
                    except Exception:
                        error_msg = resp.text[:200]
                    logger.warning("%s API error %d: %s", self.name, resp.status_code, error_msg)
                    return ProviderResponse(
                        text=f"API error ({resp.status_code}): {error_msg}",
                        provider_name=self.name,
                        success=False,
                    )

                data = resp.json()
                choices = data.get("choices", [])
                text = choices[0].get("message", {}).get("content", "") if choices else ""
                usage = data.get("usage", {})

                return ProviderResponse(
                    text=text,
                    provider_name=self.name,
                    success=True,
                    usage={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                    },
                    metadata={"latency": latency, "model": payload["model"]},
                )

            except httpx.TimeoutException:
                logger.warning("%s timeout (attempt %d/%d)", self.name, attempt + 1, self._config.retry_count + 1)
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(
                    text=f"Request timed out after {self._config.timeout}s.",
                    provider_name=self.name,
                    success=False,
                )
            except Exception as exc:
                logger.warning("%s error: %s (attempt %d/%d)", self.name, exc, attempt + 1, self._config.retry_count + 1)
                if attempt < self._config.retry_count:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return ProviderResponse(
                    text=f"Request failed: {exc}",
                    provider_name=self.name,
                    success=False,
                )

        return ProviderResponse(
            text=f"Request failed after {self._config.retry_count + 1} attempts.",
            provider_name=self.name,
            success=False,
        )

    async def vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            text="NOT_IMPLEMENTED",
            provider_name=self.name,
            success=False,
            metadata={"reason": "vision not implemented"},
        )

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)
