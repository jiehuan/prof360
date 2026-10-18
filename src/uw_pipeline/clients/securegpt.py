"""SecureGPT client: Azure OpenAI models behind the AXA XL API gateway (Phase 11, D-039).

Same interface as ClaudeClient (`complete_json`), so stages do not change. The protocol follows
the team's working client (professional-databricks utils/secure_gpt.py):

- token: POST token_url with Client_Id / Client_Secret / scope / Ocp-Apim-Subscription-Key
  headers -> access_token. Cached until shortly before it expires, refreshed after a 401.
- call: POST {base_url}/{deployment}/chat/completions?api-version=... with the bearer token and
  the subscription key; OpenAI chat format, structured output (`json_schema`, strict).

Unlike that client, TLS certificates are verified and errors are raised, not printed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel

from uw_pipeline.clients.llm import LLMError, LLMResult, LLMSetupError
from uw_pipeline.clients.openai_chat import EFFORT, chat_result, error_text

log = logging.getLogger(__name__)

TOKEN_MARGIN_SEC = 120  # refresh this long before the token expires


class GatewayEnv(BaseModel):
    token_url: str
    base_url: str


class SecureGPTConfig(BaseModel):
    default_env: str
    environments: dict[str, GatewayEnv]
    scope: str
    api_version: str | None
    send_reasoning_effort: bool
    deployments: dict[str, str]
    timeout_sec: float
    max_attempts: int
    retry_statuses: list[int]
    backoff_initial: float
    backoff_max: float


def load_securegpt_config(config_dir: Path) -> SecureGPTConfig:
    with (config_dir / "securegpt.yaml").open(encoding="utf-8") as f:
        return SecureGPTConfig.model_validate(yaml.safe_load(f))


class SecureGPTClient:
    def __init__(
        self,
        cfg: SecureGPTConfig,
        *,
        env: str,
        client_id: str,
        client_secret: str,
        subscription_key: str,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if env not in cfg.environments:
            raise LLMSetupError(f"SECUREGPT_ENV={env!r} is not in config/securegpt.yaml")
        self.cfg, self.env = cfg, cfg.environments[env]
        self._id, self._secret, self._key = client_id, client_secret, subscription_key
        self._http = http or httpx.Client(timeout=cfg.timeout_sec)
        self._sleep, self._clock = sleep, clock
        self._token: str | None = None
        self._token_until = 0.0
        # Called with (model, seconds) when the gateway throttles a completion (status 429),
        # so a shared rate limiter can hold every worker back (clients/llm_limits.py).
        self.on_throttle: Callable[[str, float], None] | None = None
        self._models = {d: m for m, d in cfg.deployments.items()}

    def close(self) -> None:
        self._http.close()

    # -- token -----------------------------------------------------------------------------

    def _get_token(self, *, force: bool = False) -> str:
        if self._token and not force and self._clock() < self._token_until:
            return self._token
        headers = {
            "Client_Id": self._id,
            "Client_Secret": self._secret,
            "scope": self.cfg.scope,
            "Cache-Control": "no-cache",
            "Ocp-Apim-Subscription-Key": self._key,
        }
        r = self._send("POST", self.env.token_url, headers=headers, what="token")
        if r.status_code in (400, 401, 403):
            raise LLMSetupError(f"SecureGPT token request rejected ({r.status_code})")
        if r.status_code >= 400:
            raise LLMError(f"SecureGPT token request failed ({r.status_code})")
        body = r.json()
        token = body.get("access_token")
        if not token:
            raise LLMSetupError("SecureGPT token response has no access_token")
        expires = float(body.get("expires_in") or 3600)
        self._token = str(token)
        self._token_until = self._clock() + max(expires - TOKEN_MARGIN_SEC, 60.0)
        return self._token

    # -- http with retries -----------------------------------------------------------------

    def _wait(self, attempt: int, r: httpx.Response | None) -> float:
        after = r.headers.get("retry-after") if r is not None else None
        if after:
            try:
                return min(float(after), self.cfg.backoff_max)
            except ValueError:
                pass
        return float(min(self.cfg.backoff_initial * (2.0**attempt), self.cfg.backoff_max))

    def _send(self, method: str, url: str, *, what: str, **kw: Any) -> httpx.Response:
        """Request with retries on transient statuses and network errors."""
        last: str = ""
        for attempt in range(self.cfg.max_attempts):
            try:
                r = self._http.request(method, url, **kw)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}"
                self._sleep(self._wait(attempt, None))
                continue
            if r.status_code in self.cfg.retry_statuses and attempt + 1 < self.cfg.max_attempts:
                last = str(r.status_code)
                log.info("SecureGPT %s: %s, retrying", what, r.status_code)
                wait = self._wait(attempt, r)
                if r.status_code == 429 and self.on_throttle and what in self._models:
                    self.on_throttle(self._models[what], wait)
                self._sleep(wait)
                continue
            return r
        raise LLMError(f"SecureGPT {what}: gave up after {self.cfg.max_attempts} attempts ({last})")

    # -- completion --------------------------------------------------------------------------

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
        deployment = self.cfg.deployments.get(model)
        if deployment is None:
            raise LLMSetupError(f"no SecureGPT deployment configured for model {model!r}")
        body: dict[str, Any] = {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            },
            "max_completion_tokens": max_tokens,
        }  # fmt: skip
        if self.cfg.send_reasoning_effort:
            body["reasoning_effort"] = EFFORT.get(effort, "medium")
        url = f"{self.env.base_url}/{deployment}/chat/completions"
        params = {"api-version": self.cfg.api_version} if self.cfg.api_version else None

        r = self._post(url, body, params)
        if r.status_code == 401:  # token expired or revoked: refresh once
            self._get_token(force=True)
            r = self._post(url, body, params)
        if r.status_code in (401, 403, 404):
            raise LLMSetupError(f"SecureGPT {deployment}: {r.status_code} {error_text(r)}")
        if r.status_code == 400:
            text = error_text(r)
            if "content_filter" in text or "content management policy" in text.lower():
                raise LLMError(f"{deployment}: input blocked by the content filter")
            if any(
                m in text.lower()
                for m in (
                    "context_length",
                    "maximum context",
                    "string_above_max_length",
                    "too many tokens",
                )
            ):
                raise LLMError(f"{deployment}: request too large: {text}")  # this item only
            raise LLMSetupError(f"SecureGPT {deployment}: 400 {text}")
        if r.status_code >= 400:
            raise LLMError(f"SecureGPT {deployment}: {r.status_code} {error_text(r)}")
        return chat_result(r.json(), deployment)

    def _post(
        self, url: str, body: dict[str, Any], params: dict[str, str] | None
    ) -> httpx.Response:
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }
        return self._send("POST", url, json=body, params=params, headers=headers,
                          what="chat completion")  # fmt: skip
