"""Shared parts of the OpenAI chat-completions format, used by the SecureGPT and Runware
clients: the effort mapping, error text and turning a response into an `LLMResult`."""

from __future__ import annotations

import json
from typing import Any

import httpx

from uw_pipeline.clients.llm import LLMError, LLMResult

# Our effort levels (shared with the Claude config) -> OpenAI reasoning_effort.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300]
    except (ValueError, AttributeError):
        return r.text[:300]


def chat_result(body: dict[str, Any], name: str) -> LLMResult:
    """The JSON object in the first choice; refusals, truncation and filtered or invalid output
    fail this item only (LLMError)."""
    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"{name} returned no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    if message.get("refusal"):
        raise LLMError(f"{name} declined: {str(message['refusal'])[:200]}")
    if finish == "length":
        raise LLMError(f"{name} hit the output token limit")
    if finish == "content_filter":
        raise LLMError(f"{name}: output blocked by the content filter")
    content = message.get("content")
    if not content:
        raise LLMError(f"{name} returned no content")
    try:
        output = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        raise LLMError(f"{name} returned invalid JSON: {exc}") from exc
    if not isinstance(output, dict):
        raise LLMError(f"{name} returned JSON that is not an object")
    usage = body.get("usage") or {}
    return LLMResult(
        output=output,
        model=str(body.get("model") or name),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def _strip_fence(text: str) -> str:
    """The answer alone: some gateways leak a reasoning model's thinking into the content,
    ending in `</think>` (DeepSeek through Runware, D-047), and some models put a ```json fence
    around JSON mode output. The answer still goes through schema validation."""
    t = text.rsplit("</think>", 1)[-1].strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
