"""LLM client interface. Pipeline code depends only on `LLMClient`; the implementation is the
SecureGPT gateway client (`clients/securegpt.py`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


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
