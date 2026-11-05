"""
CloudflareProvider — Cloudflare Workers AI (OpenAI-compatible endpoint).

Workers AI is **account-scoped**: there is no shared host root. Every request
targets ``https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1``
with a Cloudflare API token in the ``Authorization: Bearer`` header, so the
account id is part of the endpoint the factory resolves from
``AI_CLOUDFLARE_ACCOUNT_ID``. Chat Completions follows the OpenAI request and
response shape, which is why this adapter only overrides what genuinely
differs:

* the **error envelope** — Cloudflare answers ``{"success": false, "errors":
  [{"code": …, "message": …}]}`` rather than ``{"error": {...}}``;
* the **failure taxonomy** for its documented codes — a model that "No such
  model" (5007) is ``model_not_found`` (never retried), a model that "requires
  a Workers Paid plan" (5035, HTTP 403) is a request error and NOT an
  authentication rejection, and a Workers AI request timeout (408) is a
  retryable ``timeout``;
* **model listing** — the OpenAI-compatible surface exposes no ``/models``
  endpoint, so discovery uses the curated catalog instead of a doomed request.

Request construction, retries, cooldowns, capability routing and fallback all
stay where they are: ``OpenAICompatProvider`` and ``ProviderManager``.
"""
from __future__ import annotations

from typing import Any

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.defaults import get_provider_default
from backend.ai.providers.openai_compat import OpenAICompatProvider

#: Workers AI's OpenAI-compatible base URL. The account id is required: the
#: same path without it is not an endpoint.
BASE_URL_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"


def resolve_base_url(account_id: str) -> str:
    """Return the account-scoped base URL, or ``""`` without an account id."""
    account_id = (account_id or "").strip()
    return BASE_URL_TEMPLATE.format(account_id=account_id) if account_id else ""


class CloudflareProvider(OpenAICompatProvider):
    """Cloudflare Workers AI provider via its OpenAI-compatible endpoints."""

    PROVIDER_NAME = "cloudflare"
    PROVIDER_VERSION = "1.0.0"

    #: ``No such model ${model} or task`` (HTTP 400) — documented Workers AI
    #: error 5007. The model, not the provider, is wrong.
    _MODEL_ERROR_CODES = frozenset({"5007"})
    #: ``This model requires a Workers Paid plan`` (HTTP 403) — documented
    #: Workers AI error 5035. A plan restriction is NOT a rejected token.
    _PAID_MODEL_CODE = "5035"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("cloudflare")
        super().__init__(config)

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Function calling, JSON mode and streaming are Workers AI features of
        # the OPENAI-COMPATIBLE endpoint; whether a given model honors them is
        # a property of that model, exactly as with every other provider here.
        return ProviderCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_json=True,
            supports_function_call=True,
            supports_long_context=True,
        )

    def _error_fields(self, response: Any) -> dict[str, str]:
        """Read Cloudflare's error envelope.

        ``errors`` is a documented list of ``{code, message}`` objects; the
        OpenAI ``error`` object is still honored for the endpoints that answer
        with it.
        """
        try:
            data = response.json()
        except Exception:
            return {
                "message": response.text[:200],
                "code": "",
                "type": "",
                "request_id": "",
            }
        errors = data.get("errors") if isinstance(data, dict) else None
        first = errors[0] if isinstance(errors, list) and errors else None
        if not isinstance(first, dict):
            return super()._error_fields(response)
        return {
            "message": str(first.get("message") or "Unknown error"),
            "code": str(first.get("code") if first.get("code") is not None else ""),
            "type": "",
            "request_id": "",
        }

    def _failure_type_for(self, status_code: int, fields: dict[str, str]) -> str:
        """Map Workers AI's documented error codes onto the existing taxonomy."""
        code = fields.get("code", "")
        if code in self._MODEL_ERROR_CODES:
            return "model_not_found"
        if code == self._PAID_MODEL_CODE:
            return "request"
        if status_code == 408:
            return "timeout"
        return super()._failure_type_for(status_code, fields)

    async def list_models(self) -> list[dict[str, Any]]:
        """Workers AI exposes no OpenAI-style ``/models`` endpoint."""
        return []
