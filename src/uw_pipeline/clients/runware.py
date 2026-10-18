"""Runware client: OpenAI-compatible chat completions for many hosted models (D-047).

Same interface as ClaudeClient (`complete_json`), so stages do not change. POST
{base_url}/chat/completions with `Authorization: Bearer <RUNWARE_API_KEY>`; the model is a
Runware model id (deepseek:v4@flash). Structured output is a strict JSON schema, or JSON mode
with the schema in the system prompt for models that do not take one (config/runware.yaml).
The key is never logged: error messages carry the status and the gateway's error text only.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel

from uw_pipeline.clients.llm import LLMError, LLMResult, LLMSetupError
from uw_pipeline.clients.openai_chat import EFFORT, chat_result, error_text

log = logging.getLogger(__name__)

OutputMode = Literal["json_schema", "json_object"]
# Gateway messages that mean the account or key cannot run anything (not one item).
_SETUP_MESSAGES = ("credit", "invoice", "api key", "apikey", "unauthorized", "not found",
                   "unknown model", "invalid model", "not supported")  # fmt: skip
_TOO_LARGE = ("context_length", "context length", "maximum context", "too many tokens",
              "prompt is too long")  # fmt: skip


class RunwareConfig(BaseModel):
    base_url: str
    default_output: OutputMode
    output: dict[str, OutputMode]
    reasoning_effort_models: list[str]
    max_completion_tokens_prefixes: list[str] = []
    timeout_sec: float
    max_attempts: int
    retry_statuses: list[int]
    backoff_initial: float
    backoff_max: float

    def output_mode(self, model: str) -> OutputMode:
        return self.output.get(model, self.default_output)


def load_runware_config(config_dir: Path) -> RunwareConfig:
    with (config_dir / "runware.yaml").open(encoding="utf-8") as f:
        return RunwareConfig.model_validate(yaml.safe_load(f))


def schema_instruction(schema: dict[str, Any]) -> str:
    """JSON mode: the schema the answer must follow, appended to the system prompt."""
    return ("\n\nReturn only one JSON object, no other text, that conforms to this JSON schema:\n"
            + json.dumps(schema, separators=(",", ":")))  # fmt: skip


class RunwareClient:
    def __init__(
        self,
        cfg: RunwareConfig,
        *,
        api_key: str,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._key = api_key
        self._http = http or httpx.Client(timeout=cfg.timeout_sec)
        self._sleep = sleep
        # Called with (model, seconds) on status 429 (clients/llm_limits.py).
        self.on_throttle: Callable[[str, float], None] | None = None

    def close(self) -> None:
        self._http.close()

    def _wait(self, attempt: int, r: httpx.Response | None) -> float:
        after = r.headers.get("retry-after") if r is not None else None
        if after:
            try:
                return min(float(after), self.cfg.backoff_max)
            except ValueError:
                pass
        return float(min(self.cfg.backoff_initial * (2.0**attempt), self.cfg.backoff_max))

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST with retries on transient statuses and network errors."""
        url = f"{self.cfg.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(self.cfg.max_attempts):
            try:
                r = self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:  # includes timeouts on long answers
                last = type(exc).__name__
                log.info("Runware %s: %s, retrying", body["model"], last)
                self._sleep(self._wait(attempt, None))
                continue
            if r.status_code in self.cfg.retry_statuses and attempt + 1 < self.cfg.max_attempts:
                last = str(r.status_code)
                log.info("Runware %s: %s, retrying", body["model"], r.status_code)
                wait = self._wait(attempt, r)
                if r.status_code == 429 and self.on_throttle:
                    self.on_throttle(str(body["model"]), wait)
                self._sleep(wait)
                continue
            return r
        raise LLMError(f"Runware {body['model']}: gave up after {self.cfg.max_attempts} "
                       f"attempts ({last})")  # fmt: skip

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
        mode = self.cfg.output_mode(model)
        if mode == "json_schema":
            fmt: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            }
        else:
            fmt, system = {"type": "json_object"}, system + schema_instruction(schema)
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": fmt,
        }  # fmt: skip
        completion = model.startswith(tuple(self.cfg.max_completion_tokens_prefixes))
        limit = "max_completion_tokens" if completion else "max_tokens"
        body[limit] = max_tokens
        if model in self.cfg.reasoning_effort_models:
            body["reasoning_effort"] = EFFORT.get(effort, "medium")
        r = self._post(body)
        if r.status_code in (401, 402, 403, 404):
            raise LLMSetupError(f"Runware {model}: {r.status_code} {error_text(r)}")
        if r.status_code == 400:
            text = error_text(r)
            low = text.lower()
            if any(m in low for m in _TOO_LARGE):
                raise LLMError(f"Runware {model}: request too large: {text}")  # this item only
            if any(m in low for m in _SETUP_MESSAGES) or "response_format" in low:
                raise LLMSetupError(f"Runware {model}: 400 {text}")
            raise LLMError(f"Runware {model}: 400 {text}")
        if r.status_code >= 400:
            raise LLMError(f"Runware {model}: {r.status_code} {error_text(r)}")
        return chat_result(r.json(), model)
