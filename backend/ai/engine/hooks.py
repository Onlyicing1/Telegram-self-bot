"""
Hooks — lifecycle hook points for the AI Engine.

The engine invokes these hooks at fixed points during execution. The
default implementations do nothing. Future plugins subclass
``EngineHooks`` and override the methods they care about; the engine
calls them via the injected hooks object.

Lifecycle:
  before_execution(request)   — before any layer runs.
  after_prompt(prompt_package) — after the Prompt Builder produces a package.
  after_provider(response)     — after the Provider returns a response.
  after_response(result)       — after the EngineResult is assembled.
  on_error(error, stage)       — when any layer raises an exception.

All hooks are synchronous and must not raise. Any exception raised
inside a hook is caught and logged by the dispatcher so the engine
never crashes due to a hook.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.engine.result import EngineResult
from backend.ai.prompt.builder import PromptPackage
from backend.ai.providers.base import ProviderResponse
from backend.ai.session.request import AIRequest

logger = logging.getLogger(__name__)


class EngineHooks:
    """Base hook implementations — all no-ops. Subclass to extend."""

    def before_execution(self, request: AIRequest) -> None:
        pass

    def after_prompt(self, prompt_package: PromptPackage) -> None:
        pass

    def after_provider(self, response: ProviderResponse) -> None:
        pass

    def after_response(self, result: EngineResult) -> None:
        pass

    def on_error(self, error: str, stage: str) -> None:
        pass


class _NoopHooks(EngineHooks):
    """Concrete no-op singleton used when no hooks are injected."""

    __slots__ = ()


NOOP_HOOKS = _NoopHooks()


def safe_call(hooks: EngineHooks, method_name: str, *args: Any, **kwargs: Any) -> None:
    """Invoke a hook method, catching and logging any exception."""
    method = getattr(hooks, method_name, None)
    if method is None:
        return
    try:
        method(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — hooks must never crash the engine
        logger.warning("EngineHooks.%s raised: %r", method_name, exc)
