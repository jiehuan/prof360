"""LLM client interface and the Claude implementation.

Pipeline code depends only on `LLMClient`; switching to the Secure GPT gateway later means
adding another implementation here.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

log = logging.getLogger(__name__)

# Server-side fallback: on a safety decline the API retries on a fallback model it picks by
# refusal category (Claude API only).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that reject the `fallbacks` parameter with a 400 (no server-side fallback).
NO_FALLBACK_PREFIXES = ("claude-haiku-",)


class LLMError(Exception):
    """The model produced no usable output (refusal, truncation, invalid JSON)."""


class LLMSetupError(Exception):
    """Credentials, permissions or the request itself are wrong for every call (not one item);
    callers stop using the LLM for the run."""


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    model: str  # the model that actually served the request
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    def complete_json(
        self,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> LLMResult: ...


class ClaudeClient:
    def __init__(
        self, api_key: str, *, workspace_id: str | None = None, timeout: float = 600.0
    ) -> None:
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._make: Callable[[], Any] = lambda: anthropic.Anthropic(
            api_key=api_key, default_headers=headers, timeout=timeout
        )
        self._client = self._make()
        self._deadline = timeout

    def _within_deadline(self, call: Callable[[Any], Any]) -> Any:
        """Run one API call, giving up after `timeout` seconds in all, retries included (the
        SDK's timeout bounds each socket read, not the call). A call past the deadline is left
        on its own thread and later calls get a fresh client. Timers count only while the
        machine is awake: in Phase 16 a laptop's idle sleep froze calls mid-flight (run
        locally with `caffeinate -i`)."""
        client = self._client
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = call(client)
            except BaseException as exc:  # handed to the caller below
                box["error"] = exc

        # Copy the context so log lines keep the run / ticker / stage fields.
        worker = threading.Thread(target=contextvars.copy_context().run, args=(run,),
                                  name="claude-call", daemon=True)  # fmt: skip
        worker.start()
        worker.join(self._deadline)
        if worker.is_alive():
            self._client = self._make()
            raise LLMError(f"no answer within {self._deadline:.0f} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def complete_json(
        self,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        system: str,
        user: str,
        schema: dict[str, Any],
    ) -> LLMResult:
        # Plain dicts below; the SDK's TypedDict overloads are too narrow for mypy here.
        fallback: dict[str, Any] = (
            {} if model.startswith(NO_FALLBACK_PREFIXES)
            else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        )  # fmt: skip
        response = self._within_deadline(
            lambda client: client.beta.messages.create(
                model=model,
                max_tokens=max_tokens,
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **fallback,
            )
        )  # fmt: skip
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"{model} declined: {getattr(details, 'category', None)}")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{model} hit max_tokens={max_tokens}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"{model} returned no text block")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{model} returned invalid JSON: {exc}") from exc
        return LLMResult(
            output=output,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
