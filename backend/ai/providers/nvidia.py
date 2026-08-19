"""
NVIDIAProvider — NVIDIA NIM / NVIDIA Build adapter (OpenAI-compatible).

NVIDIA exposes an OpenAI-compatible chat-completions API at
``https://integrate.api.nvidia.com/v1``. It is treated as backup/evaluation
capacity rather than guaranteed permanent free infrastructure.
"""
from __future__ import annotations

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider


class NVIDIAProvider(OpenAICompatProvider):
    PROVIDER_NAME = "nvidia"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("nvidia")
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
        )
