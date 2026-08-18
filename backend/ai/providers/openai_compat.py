"""
OpenAI-compatible async provider base.

All providers that follow the OpenAI chat completions API format
inherit from this class. Handles async HTTP, retry, rate limits, timeouts.
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
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = tools

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
                        metadata={"http_status": 429, "retry_after": retry_after},
                    )

                if resp.status_code >= 400:
                    error_msg = "Unknown error"
                    provider_error_code = ""
                    provider_error_type = ""
                    try:
                        error_data = resp.json()
                        err_obj = error_data.get("error", {})
                        error_msg = err_obj.get("message", error_msg)
                        provider_error_code = str(err_obj.get("code", ""))
                        provider_error_type = err_obj.get("type", "")
                    except Exception:
                        error_msg = resp.text[:200]
                    logger.warning("%s API error %d: %s", self.name, resp.status_code, error_msg)
                    return ProviderResponse(
                        text=f"API error ({resp.status_code}): {error_msg}",
                        provider_name=self.name,
                        success=False,
                        metadata={"http_status": resp.status_code, "provider_error_code": provider_error_code, "provider_error_type": provider_error_type},
                    )

                data = resp.json()
                choices = data.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
                text = message.get("content", "") if choices else ""
                finish_reason = choices[0].get("finish_reason", "") if choices else ""
                usage = data.get("usage", {})

                tool_calls: list[dict[str, Any]] = []
                raw_tool_calls = message.get("tool_calls", []) if choices else []
                for tc in raw_tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    args_raw = fn.get("arguments", "{}")
                    malformed = False
                    arguments_error = ""
                    if isinstance(args_raw, str):
                        try:
                            arguments = json.loads(args_raw) if args_raw.strip() else {}
                        except (json.JSONDecodeError, TypeError) as exc:
                            # Malformed JSON must NOT silently become a valid
                            # empty argument object — the executor receives a
                            # structured failure instead of executing with {}.
                            arguments = {}
                            malformed = True
                            arguments_error = f"malformed JSON arguments: {exc}"
                    elif isinstance(args_raw, dict):
                        arguments = args_raw
                    else:
                        arguments = {}
                        malformed = True
                        arguments_error = f"unexpected arguments type: {type(args_raw).__name__}"
                    entry: dict[str, Any] = {
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": arguments,
                    }
                    if malformed:
                        entry["malformed_arguments"] = True
                        entry["arguments_error"] = arguments_error
                    tool_calls.append(entry)

                if not text and finish_reason:
                    if finish_reason == "length":
                        text = "Response truncated due to token limit."
                    elif finish_reason == "content_filter":
                        text = "Response blocked by content filter."

                return ProviderResponse(
                    text=text,
                    provider_name=self.name,
                    success=True,
                    tool_calls=tool_calls,
                    usage={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    metadata={"latency": latency, "model": payload["model"], "finish_reason": finish_reason},
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
                    metadata={"error_type": "timeout"},
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
                    metadata={"error_type": type(exc).__name__},
                )

        return ProviderResponse(
            text=f"Request failed after {self._config.retry_count + 1} attempts.",
            provider_name=self.name,
            success=False,
            metadata={"error_type": "retry_exhausted"},
        )

    async def vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            text="NOT_IMPLEMENTED",
            provider_name=self.name,
            success=False,
            metadata={"reason": "vision not implemented"},
        )

    async def list_models(self) -> list[dict[str, Any]]:
        if not self._config.api_key or not self._config.enabled:
            return []
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._config.timeout,
                headers={"Authorization": f"Bearer {self._config.api_key}", "Content-Type": "application/json"},
            )
        url = f"{self._config.base_url}/models"
        try:
            resp = await self._http_client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("data", [])
        except Exception:
            return []

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)
