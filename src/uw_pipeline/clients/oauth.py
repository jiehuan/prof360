"""Client-credentials tokens; no response bodies or transport exception text escape here."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote_plus

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base


def https_endpoint(value: str) -> str:
    """Endpoints cannot embed credentials, queries or fragments; parameters are supplied later."""
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("must be an absolute HTTPS endpoint") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("must be an HTTPS endpoint without credentials, query or fragment")
    return value


class ClientCredentialsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_url: str
    scope: str = Field(min_length=1)
    token_auth_method: Literal["client_secret_post", "client_secret_basic", "gateway_headers"] = (
        "client_secret_post"
    )
    # Corporate token endpoints can themselves sit behind APIM. Disable for an IdP that
    # must not receive the API subscription key.
    subscription_key_on_token: bool = True

    _token_endpoint = field_validator("token_url")(https_endpoint)

    @field_validator("scope")
    @classmethod
    def _scope_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scope must not be blank")
        return value.strip()


class OAuthError(Exception):
    pass


class OAuthSetupError(OAuthError):
    pass


class OAuthTransientError(OAuthError):
    pass


class ClientCredentialsTokenProvider:
    def __init__(
        self,
        cfg: ClientCredentialsConfig,
        *,
        client_id: str,
        client_secret: str,
        subscription_key: str,
        http: httpx.Client,
        max_attempts: int = 3,
        wait: wait_base | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._id, self._secret, self._key = client_id, client_secret, subscription_key
        self._http, self._clock = http, clock
        self._max_attempts = max_attempts
        self._wait = wait if wait is not None else wait_exponential_jitter(initial=2, max=60)
        self._token: str | None = None
        self._token_until = 0.0
        self._issued_tokens: set[str] = set()
        self._lock = threading.RLock()

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        # Keep old tokens redacted too, including a token replaced after a 401.
        with self._lock:
            return (self._id, self._secret, self._key, *self._issued_tokens)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Ocp-Apim-Subscription-Key": self._key,
        }

    def get_token(self, *, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and self._clock() < self._token_until:
                return self._token
            # Do not reuse a token after a failed forced renewal.
            self._token, self._token_until = None, 0.0
            retrying = Retrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=self._wait,
                retry=retry_if_exception_type(OAuthTransientError),
                reraise=True,
            )
            response, started = retrying(self._request_once)
            try:
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError
                token = body.get("access_token")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
                    raise ValueError
                if str(body.get("token_type", "Bearer")).lower() != "bearer":
                    raise ValueError
                if isinstance(body["expires_in"], bool):
                    raise ValueError
                lifetime = float(body["expires_in"])
                if not math.isfinite(lifetime) or lifetime <= 0:
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError):
                raise OAuthSetupError("Alpha Vantage OAuth token response is invalid") from None
            # A fixed minimum lifetime could keep a short-lived token past its expiry.
            if self._clock() >= started + lifetime:
                raise OAuthSetupError("Alpha Vantage OAuth token expired before receipt")
            margin = min(120.0, lifetime * 0.1)
            self._token = token
            self._issued_tokens.add(token)
            self._token_until = started + lifetime - margin
            return token

    def _request_once(self) -> tuple[httpx.Response, float]:
        headers = {"Cache-Control": "no-cache"}
        if self.cfg.subscription_key_on_token:
            headers["Ocp-Apim-Subscription-Key"] = self._key
        data = {"grant_type": "client_credentials"}
        auth: httpx.Auth = httpx.Auth()
        if self.cfg.token_auth_method == "gateway_headers":
            headers.update(Client_Id=self._id, Client_Secret=self._secret, scope=self.cfg.scope)
        else:
            data["scope"] = self.cfg.scope
            if self.cfg.token_auth_method == "client_secret_basic":
                # RFC 6749 2.3.1 requires form-encoding each credential before Basic encoding.
                auth = httpx.BasicAuth(quote_plus(self._id), quote_plus(self._secret))
            else:
                data.update(client_id=self._id, client_secret=self._secret)
        started = self._clock()
        try:
            response = self._http.post(
                self.cfg.token_url, headers=headers, data=data, auth=auth, follow_redirects=False
            )
        except httpx.TransportError as exc:
            raise OAuthTransientError(
                f"Alpha Vantage OAuth request failed ({type(exc).__name__})"
            ) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise OAuthTransientError(f"Alpha Vantage OAuth returned HTTP {response.status_code}")
        if response.status_code in (400, 401, 403):
            raise OAuthSetupError(
                f"Alpha Vantage OAuth rejected credentials ({response.status_code})"
            )
        if not 200 <= response.status_code < 300:
            raise OAuthError(f"Alpha Vantage OAuth returned HTTP {response.status_code}")
        return response, started
