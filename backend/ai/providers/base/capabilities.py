"""
ProviderCapabilities — declares what a provider can actually do.

Every provider exposes a ``capabilities`` property returning a frozen
``ProviderCapabilities`` instance. The Settings UI (and any future
feature-gating code) reads these flags to decide which controls to
show, which actions to enable, and which request paths to take.

All fields default to ``False`` so a new provider that forgets to set
them simply appears as a minimal text-only provider — never as one
that claims to support features it doesn't.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_streaming: bool = False
    supports_images: bool = False
    supports_reasoning: bool = False
    supports_tools: bool = False
    supports_json: bool = False
    supports_function_call: bool = False
    supports_long_context: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "supports_streaming": self.supports_streaming,
            "supports_images": self.supports_images,
            "supports_reasoning": self.supports_reasoning,
            "supports_tools": self.supports_tools,
            "supports_json": self.supports_json,
            "supports_function_call": self.supports_function_call,
            "supports_long_context": self.supports_long_context,
        }
