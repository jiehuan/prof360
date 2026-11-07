"""Async SecureGPT client for the BFF (D-091): Azure OpenAI deployments behind the AXA XL API
gateway, the same protocol as the pipeline's client (D-039):

- token: POST token_url with Client_Id / Client_Secret / scope / Ocp-Apim-Subscription-Key
  headers -> access_token, cached until shortly before it expires and refreshed after a 401;
- call: POST {base_url}/{deployment}/chat/completions?api-version=... with the bearer token
  and the subscription key, OpenAI chat format, structured output (json_schema, strict).

Credentials come from the BFF's environment (SecureGPTSettings.from_env); they are never
logged or put in an error message. TLS certificates are verified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, SecretStr

log = logging.getLogger(__name__)

# The gateway per environment (as config/securegpt.yaml in the pipeline; a test keeps them equal).
GATEWAYS: dict[str, dict[str, str]] = {
    "dv": {
        "token_url": "https://gateway-dev.api.axaxl.com/axaonelogin/v1/as/token.oauth2",
        "base_url": "https://gateway-dev.api.axaxl.com/securegptmodelhub/v1/providers/openai/deployments",
    },
    "pp": {
        "token_url": "https://gateway-preprod.api.axaxl.com/axaonelogin/v1/as/token.oauth2",
        "base_url": "https://gateway-preprod.api.axaxl.com/securegptmodelhub/v1/providers/openai/deployments",
    },
    "pr": {
        "token_url": "https://gateway.api.axaxl.com/axaonelogin/v1/as/token.oauth2",
        "base_url": "https://gateway.api.axaxl.com/securegptmodelhub/v1/providers/openai/deployments",
    },
}  # fmt: skip
SCOPE = "urn:grp:chatgpt"
API_VERSION = "2025-04-01-preview"
MODELS = ("gpt-6-luna-2026-09-22", "gpt-6-sol-2026-09-22")
DEFAULT_MODEL = MODELS[0]
TOKEN_MARGIN_SEC = 120  # refresh this long before the token expires


class GatewayError(RuntimeError):
    """kind: setup (credentials, deployment, request rejected: fix the configuration),
    transient (timeouts, throttling, 5xx after retries: try again later) or output (the
    model's reply could not be used: refused, cut off, filtered or not JSON)."""

    def __init__(self, kind: Literal["setup", "transient", "output"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


class SecureGPTSettings(BaseModel):
    env: str = "dv"
    client_id: str
    client_secret: SecretStr
    subscription_key: SecretStr
    token_url: str | None = None  # default: GATEWAYS[env]
    base_url: str | None = None
    scope: str = SCOPE
    api_version: str = API_VERSION
    timeout_sec: float = 60.0
    max_attempts: int = 3
    retry_statuses: tuple[int, ...] = (408, 429, 500, 502, 503, 504)
    backoff_initial: float = 1.0
    backoff_max: float = 20.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **over: Any) -> SecureGPTSettings:
        """SECUREGPT_CLIENT_ID, SECUREGPT_CLIENT_SECRET, SECUREGPT_SUBSCRIPTION_KEY and
        SECUREGPT_ENV (dv | pp | pr), the names the pipeline uses."""
        env = os.environ if environ is None else environ
        missing = [k for k in ("SECUREGPT_CLIENT_ID", "SECUREGPT_CLIENT_SECRET",
                               "SECUREGPT_SUBSCRIPTION_KEY") if not env.get(k)]  # fmt: skip
        if missing:
            raise GatewayError("setup", f"environment variables not set: {', '.join(missing)}")
        return cls(
            env=env.get("SECUREGPT_ENV") or "dv",
            client_id=env["SECUREGPT_CLIENT_ID"],
            client_secret=SecretStr(env["SECUREGPT_CLIENT_SECRET"]),
            subscription_key=SecretStr(env["SECUREGPT_SUBSCRIPTION_KEY"]),
            **over,
        )

    def urls(self) -> tuple[str, str]:
        gateway = GATEWAYS.get(self.env)
        token, base = self.token_url, self.base_url
        if gateway is None and not (token and base):
            raise GatewayError("setup", f"SECUREGPT_ENV={self.env!r} is not dv, pp or pr")
        return (token or (gateway or {})["token_url"], base or (gateway or {})["base_url"])


@dataclass(frozen=True)
class Completion:
    output: dict[str, Any]
    raw: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class AsyncSecureGPT:
    """One per BFF process; safe to share between requests. Close it on shutdown."""

    def __init__(
        self,
        settings: SecureGPTSettings,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.s = settings
        self._token_url, self._base_url = settings.urls()
        self._http = http or httpx.AsyncClient(timeout=settings.timeout_sec)
        self._sleep, self._clock = sleep, clock
        self._token: str | None = None
        self._token_until = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- token -------------------------------------------------------------------------------

    async def _get_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if self._token and not force and self._clock() < self._token_until:
                return self._token
            headers = {
                "Client_Id": self.s.client_id,
                "Client_Secret": self.s.client_secret.get_secret_value(),
                "scope": self.s.scope,
                "Cache-Control": "no-cache",
                "Ocp-Apim-Subscription-Key": self.s.subscription_key.get_secret_value(),
            }
            r = await self._send("POST", self._token_url, what="token", headers=headers)
            if r.status_code in (400, 401, 403):
                raise GatewayError("setup", f"SecureGPT token request rejected ({r.status_code})")
            if r.status_code >= 400:
                raise GatewayError("transient", f"SecureGPT token request failed ({r.status_code})")
            body = r.json()
            token = body.get("access_token")
            if not token:
                raise GatewayError("setup", "SecureGPT token response has no access_token")
            expires = float(body.get("expires_in") or 3600)
            self._token = str(token)
            self._token_until = self._clock() + max(expires - TOKEN_MARGIN_SEC, 60.0)
            return self._token

    # -- http with retries -------------------------------------------------------------------

    def _wait(self, attempt: int, r: httpx.Response | None) -> float:
        after = r.headers.get("retry-after") if r is not None else None
        if after:
            try:
                return min(float(after), self.s.backoff_max)
            except ValueError:
                pass
        return float(min(self.s.backoff_initial * (2.0**attempt), self.s.backoff_max))

    async def _send(self, method: str, url: str, *, what: str, **kw: Any) -> httpx.Response:
        last = ""
        for attempt in range(self.s.max_attempts):
            r: httpx.Response | None = None
            try:
                r = await self._http.request(method, url, **kw)
            except httpx.TransportError as exc:  # includes timeouts
                last = type(exc).__name__
            else:
                if r.status_code not in self.s.retry_statuses:
                    return r
                last = str(r.status_code)
                if attempt + 1 == self.s.max_attempts:
                    return r
            log.info("SecureGPT %s: %s, retrying", what, last)
            await self._sleep(self._wait(attempt, r))
        raise GatewayError("transient", f"SecureGPT {what}: gave up after "
                           f"{self.s.max_attempts} attempts ({last})")  # fmt: skip

    # -- completion --------------------------------------------------------------------------

    async def complete_json(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        schema: Mapping[str, Any],
        max_tokens: int = 1500,
    ) -> Completion:
        url = f"{self._base_url}/{model}/chat/completions"
        body = {
            "messages": list(messages),
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True,
                                                "schema": dict(schema)}},
            "max_completion_tokens": max_tokens,
        }  # fmt: skip
        started = self._clock()
        r = await self._post(url, body)
        if r.status_code == 401:  # token expired or revoked: refresh once
            await self._get_token(force=True)
            r = await self._post(url, body)
        latency = int((self._clock() - started) * 1000)
        if r.status_code in (401, 403, 404):
            raise GatewayError("setup", f"SecureGPT {model}: {r.status_code} {_error_text(r)}")
        if r.status_code == 400:
            text = _error_text(r)
            if "content_filter" in text or "content management policy" in text.lower():
                raise GatewayError("output", f"{model}: input blocked by the content filter")
            raise GatewayError("setup", f"SecureGPT {model}: 400 {text}")
        if r.status_code >= 400:
            raise GatewayError("transient", f"SecureGPT {model}: {r.status_code} {_error_text(r)}")
        return _completion(r.json(), model, latency)

    async def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "Ocp-Apim-Subscription-Key": self.s.subscription_key.get_secret_value(),
            "Authorization": f"Bearer {await self._get_token()}",
            "Content-Type": "application/json",
        }
        return await self._send("POST", url, what="chat completion", json=body,
                                params={"api-version": self.s.api_version},
                                headers=headers)  # fmt: skip


def _error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300]
    except (ValueError, AttributeError):
        return r.text[:300]


def _completion(body: dict[str, Any], model: str, latency_ms: int) -> Completion:
    choices = body.get("choices") or []
    if not choices:
        raise GatewayError("output", f"{model} returned no choices")
    message = choices[0].get("message") or {}
    finish = choices[0].get("finish_reason")
    if message.get("refusal"):
        raise GatewayError("output", f"{model} declined: {str(message['refusal'])[:200]}")
    if finish == "length":
        raise GatewayError("output", f"{model} hit the output token limit")
    if finish == "content_filter":
        raise GatewayError("output", f"{model}: output blocked by the content filter")
    content = (message.get("content") or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        output = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GatewayError("output", f"{model} returned invalid JSON: {exc}") from exc
    if not isinstance(output, dict):
        raise GatewayError("output", f"{model} returned JSON that is not an object")
    usage = body.get("usage") or {}
    return Completion(output=output, raw=content, model=str(body.get("model") or model),
                      input_tokens=int(usage.get("prompt_tokens") or 0),
                      output_tokens=int(usage.get("completion_tokens") or 0),
                      latency_ms=latency_ms)  # fmt: skip
