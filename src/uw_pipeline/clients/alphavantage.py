"""Alpha Vantage client: rate limit, daily quota, raw landing.

Alpha Vantage reports errors as HTTP 200 with a JSON body holding "Information", "Note" or
"Error Message" instead of data; these are turned into typed exceptions. The API key is never
written to storage, logs or URLs kept for lineage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base

from uw_pipeline.clients.oauth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    OAuthError,
    https_endpoint,
)
from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import Storage
from uw_pipeline.settings import Settings
from uw_pipeline.yaml_load import safe_load

log = logging.getLogger(__name__)

AV_URL = "https://www.alphavantage.co/query"


class AlphaVantageError(Exception):
    pass


class AlphaVantageSetupError(AlphaVantageError, ValueError):
    """Missing credentials or invalid route configuration, reported without values."""


class PremiumRequiredError(AlphaVantageError):
    """The endpoint needs a paid plan (e.g. NEWS_SENTIMENT on a free key)."""


class QuotaExhaustedError(AlphaVantageError):
    """Daily request quota used up (ours or Alpha Vantage's); resume on the next run."""


class TransientError(AlphaVantageError):
    pass


class InvalidCallError(AlphaVantageError):
    """ "Invalid API call": also returned now and then for a valid request (prod run 5: 9 of 15
    EARNINGS calls succeeded when asked again), so it is retried before it fails (D-097)."""


class DirectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_url: str = AV_URL

    _query_endpoint = field_validator("query_url")(https_endpoint)


class AlphaVantageGatewayEnv(DirectConfig):
    query_url: str
    oauth: ClientCredentialsConfig


class APIMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_env: str = "dv"
    environments: dict[str, AlphaVantageGatewayEnv] = Field(default_factory=dict)


class AlphaVantageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direct: DirectConfig = Field(default_factory=DirectConfig)
    apim: APIMConfig = Field(default_factory=APIMConfig)
    timeout_sec: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1)


def load_alphavantage_config(config_dir: Path) -> AlphaVantageConfig:
    path = config_dir / "alphavantage.yaml"
    if not path.exists():
        return AlphaVantageConfig()  # older config mounts still support direct
    try:
        return AlphaVantageConfig.model_validate(safe_load(path.read_text(encoding="utf-8")))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise AlphaVantageSetupError(
            "invalid config/alphavantage.yaml fields: " + ", ".join(fields)
        ) from None
    except (OSError, UnicodeError, yaml.YAMLError):
        raise AlphaVantageSetupError("cannot read config/alphavantage.yaml") from None


def validate_alphavantage_settings(
    settings: Settings, cfg: AlphaVantageConfig
) -> AlphaVantageGatewayEnv | None:
    """Offline credential/selected-route preflight, shared with the deployment guard."""
    required: dict[str, str | SecretStr | None] = {
        "ALPHAVANTAGE_API_KEY": settings.alphavantage_api_key
    }
    if settings.alphavantage_mode == "apim":
        required.update(
            ALPHAVANTAGE_CLIENT_ID=settings.alphavantage_client_id,
            ALPHAVANTAGE_CLIENT_SECRET=settings.alphavantage_client_secret,
            ALPHAVANTAGE_SUBSCRIPTION_KEY=settings.alphavantage_subscription_key,
        )
    missing = []
    for name, value in required.items():
        plain = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not plain or not plain.strip() or "REPLACE_WITH_" in plain:
            missing.append(name)
    if missing:
        raise AlphaVantageSetupError("missing Alpha Vantage credentials: " + ", ".join(missing))
    if settings.alphavantage_mode == "direct":
        return None
    env = cfg.apim.environments.get(settings.alphavantage_env or cfg.apim.default_env)
    if env is None:
        raise AlphaVantageSetupError("ALPHAVANTAGE_ENV is not in config/alphavantage.yaml")
    if any(
        "REPLACE_WITH_" in value or ".invalid" in value
        for value in (env.query_url, env.oauth.token_url, env.oauth.scope)
    ):
        raise AlphaVantageSetupError("selected Alpha Vantage APIM environment has placeholders")
    return env


def make_alphavantage_client(settings: Settings, storage: Storage) -> AlphaVantageClient:
    cfg = load_alphavantage_config(settings.config_dir)
    env = validate_alphavantage_settings(settings, cfg)
    assert settings.alphavantage_api_key is not None  # checked above
    http = httpx.Client(timeout=cfg.timeout_sec)
    oauth = None
    if env is not None:
        assert settings.alphavantage_client_id is not None
        assert settings.alphavantage_client_secret is not None
        assert settings.alphavantage_subscription_key is not None
        oauth = ClientCredentialsTokenProvider(
            env.oauth,
            client_id=settings.alphavantage_client_id,
            client_secret=settings.alphavantage_client_secret.get_secret_value(),
            subscription_key=settings.alphavantage_subscription_key.get_secret_value(),
            http=http,
            max_attempts=cfg.max_attempts,
        )
    return AlphaVantageClient(
        settings.alphavantage_api_key.get_secret_value(), storage,
        query_url=env.query_url if env is not None else cfg.direct.query_url,
        oauth=oauth, http=http, max_attempts=cfg.max_attempts,
        rate_limiter=TokenBucket(rate=settings.alphavantage_calls_per_minute / 60),
        daily_quota=settings.alphavantage_daily_quota or None,
    )  # fmt: skip


def _utc_today() -> date:
    return datetime.now(UTC).date()


def classify(payload: Any, secret: str = "", *, secrets: tuple[str, ...] = ()) -> None:
    """Raise if an HTTP-200 body is an Alpha Vantage error message.

    Alpha Vantage echoes the API key in some messages ("We have detected your API key as ..."),
    so `secret` is masked before the message goes anywhere."""
    if not isinstance(payload, dict):
        return
    msg = payload.get("Information") or payload.get("Note") or payload.get("Error Message")
    if msg is None or len(payload) > 1:
        return
    msg = str(msg)
    for value in sorted({secret, *secrets} - {""}, key=len, reverse=True):
        msg = msg.replace(value, "***")
    low = msg.lower()
    # Rate-limit messages also mention premium plans, so check them first.
    if "rate limit" in low or "requests per day" in low or "call frequency" in low:
        raise QuotaExhaustedError(msg)
    if "premium endpoint" in low or "premium" in low:
        raise PremiumRequiredError(msg)
    if "invalid api call" in low:
        raise InvalidCallError(msg)
    raise AlphaVantageError(msg)


class AlphaVantageClient:
    def __init__(
        self,
        api_key: str,
        storage: Storage,
        *,
        query_url: str = AV_URL,
        oauth: ClientCredentialsTokenProvider | None = None,
        rate_limiter: TokenBucket | None = None,
        daily_quota: int | None = 25,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        wait: wait_base | None = None,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        if not api_key.strip():
            raise AlphaVantageSetupError("ALPHAVANTAGE_API_KEY must not be blank")
        self._key = api_key
        self._url, self._oauth = https_endpoint(query_url), oauth
        self._storage = storage
        self._limiter = rate_limiter or TokenBucket(rate=5 / 60)
        self._quota = daily_quota
        self._http = http or httpx.Client(timeout=60)
        self._max_attempts = max_attempts
        self._wait = wait or wait_exponential_jitter(initial=2, max=60)
        self._today = today
        # The news stage fetches for several companies at once (D-079): the quota check and
        # the call count are one step, so parallel calls neither overrun the quota nor lose a
        # count. The rate limiter and the OAuth token provider are thread-safe on their own,
        # so the requests themselves run side by side.
        self._quota_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AlphaVantageClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- quota ---------------------------------------------------------------------------------

    def _quota_key(self) -> str:
        return f"av/_quota/{self._today().isoformat()}.json"

    def calls_today(self) -> int:
        key = self._quota_key()
        if not self._storage.exists(key):
            return 0
        used: int = json.loads(self._storage.read_bytes(key))["calls"]
        return used

    def _count_call(self) -> None:
        self._storage.write_bytes(
            self._quota_key(), json.dumps({"calls": self.calls_today() + 1}).encode()
        )

    def _check_quota(self) -> None:
        if self._quota is not None and self.calls_today() >= self._quota:
            raise QuotaExhaustedError(f"daily quota of {self._quota} Alpha Vantage calls used")

    # -- transport -----------------------------------------------------------------------------

    def _get_once(self, params: dict[str, str]) -> httpx.Response:
        self._check_quota()  # before asking for a token
        try:
            headers = self._oauth.headers() if self._oauth is not None else {}
        except OAuthError as exc:
            raise AlphaVantageError(str(exc)) from None
        # Count attempted data calls conservatively, including transport failures and 401s.
        with self._quota_lock:
            self._check_quota()
            self._count_call()
        self._limiter.acquire()
        try:
            response = self._http.get(
                self._url,
                params={**params, "apikey": self._key},
                headers=headers,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            raise TransientError(f"Alpha Vantage request failed ({type(exc).__name__})") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"HTTP {response.status_code}")
        return response

    def fetch(
        self,
        params: dict[str, str],
        key: str,
        *,
        force: bool = False,
        keep: Callable[[Any], bool] | None = None,
    ) -> Any:
        """GET with retries. The response is landed under `key` and reused unless `force`;
        with `keep`, only responses it accepts are landed (others are re-fetched next time)."""
        if not force and self._storage.exists(key):
            return json.loads(self._storage.read_bytes(key))
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((httpx.TransportError, TransientError)),
            reraise=True,
        )
        again = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type(InvalidCallError),
            reraise=True,
        )
        payload, data = again(self._payload, params, retrying)
        # A misconfigured gateway can return a successful echo/debug response. Refuse to land
        # it rather than altering raw evidence or persisting credentials in the raw cache.
        serialized = json.dumps(payload, ensure_ascii=False)
        private_key = self._key if self._key != "demo" else ""
        # Short identifiers also occur in normal financial prose; match them only as entire
        # JSON strings. For real long credentials, also detect echoes embedded in longer text.
        if any(
            value
            and (
                json.dumps(value, ensure_ascii=False) in serialized
                or (len(value) >= 8 and json.dumps(value, ensure_ascii=False)[1:-1] in serialized)
            )
            for value in (private_key, *self._secrets())
        ):
            raise AlphaVantageError("Alpha Vantage response contains credentials")
        if keep is None or keep(payload):
            self._storage.write_bytes(key, data)
        return payload

    def _secrets(self) -> tuple[str, ...]:
        return tuple(self._oauth.sensitive_values) if self._oauth is not None else ()

    def _payload(self, params: dict[str, str], retrying: Retrying) -> tuple[Any, bytes]:
        """One request (transport retries included), checked and parsed: (payload, body)."""
        response = retrying(self._get_once, params)
        if response.status_code == 401 and self._oauth is not None:
            # Only one renewal/replay per fetch, even if data requests have transient retries.
            self._check_quota()
            try:
                self._oauth.get_token(force=True)
            except OAuthError as exc:
                raise AlphaVantageError(str(exc)) from None
            response = retrying(self._get_once, params)
        if not 200 <= response.status_code < 300:
            # raise_for_status() includes the URL (and API key); gateway bodies may echo tokens.
            raise AlphaVantageError(f"Alpha Vantage returned HTTP {response.status_code}")
        data = response.content
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            raise AlphaVantageError("Alpha Vantage returned invalid JSON") from None
        classify(payload, self._key, secrets=self._secrets())
        if isinstance(payload, dict) and "error" in payload:
            raise AlphaVantageError("Alpha Vantage returned an error response")
        return payload, data

    # -- endpoints -----------------------------------------------------------------------------

    def news(self, ticker: str, time_from: datetime, *, force: bool = False) -> tuple[Any, str]:
        """NEWS_SENTIMENT for one ticker since `time_from` (oldest first, up to 1000 items).
        Returns (payload, raw landing key)."""
        stamp = time_from.strftime("%Y%m%dT%H%M")
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "time_from": stamp,
            "sort": "EARLIEST",
            "limit": "1000",
        }
        key = f"av/news/{ticker}/{self._today().isoformat()}-from-{stamp}.json"
        return self.fetch(params, key, force=force), key

    def transcript(self, symbol: str, quarter: str, *, force: bool = False) -> tuple[Any, str]:
        """EARNINGS_CALL_TRANSCRIPT, quarter like "2026Q2". Returns (payload, raw landing key).
        Transcripts don't change once published, so they are cached without a date; an empty
        one is not cached."""
        params = {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol, "quarter": quarter}
        key = f"av/transcripts/{symbol}/{quarter}.json"
        payload = self.fetch(params, key, force=force, keep=lambda p: bool(p.get("transcript")))
        return payload, key

    def daily_adjusted(self, symbol: str, *, force: bool = False) -> tuple[Any, str]:
        """TIME_SERIES_DAILY_ADJUSTED, full history (paid plan; Phase 15a). Landed once a day."""
        params = {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol,
                  "outputsize": "full"}  # fmt: skip
        key = f"av/daily/{symbol}/{self._today().isoformat()}.json"
        payload = self.fetch(params, key, force=force,
                             keep=lambda p: bool(p.get("Time Series (Daily)")))  # fmt: skip
        return payload, key

    def earnings(self, symbol: str, *, force: bool = False) -> tuple[Any, str]:
        """EARNINGS: quarterly reported and estimated EPS (Phase 15a). Landed once a day."""
        params = {"function": "EARNINGS", "symbol": symbol}
        key = f"av/earnings/{symbol}/{self._today().isoformat()}.json"
        payload = self.fetch(params, key, force=force,
                             keep=lambda p: bool(p.get("quarterlyEarnings")))  # fmt: skip
        return payload, key


def public_url(params: dict[str, str]) -> str:
    """Request URL for lineage, without the API key."""
    private = {"apikey", "access_token", "client_id", "client_secret",
               "ocp-apim-subscription-key", "authorization"}  # fmt: skip
    public = {k: v for k, v in params.items() if k.lower() not in private}
    return str(httpx.URL(AV_URL, params=public))


def content_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
