"""Alpha Vantage client: rate limit, daily quota, raw landing.

Alpha Vantage reports errors as HTTP 200 with a JSON body holding "Information", "Note" or
"Error Message" instead of data; these are turned into typed exceptions. The API key is never
written to storage, logs or URLs kept for lineage.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base

from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import Storage

log = logging.getLogger(__name__)

AV_URL = "https://www.alphavantage.co/query"


class AlphaVantageError(Exception):
    pass


class PremiumRequiredError(AlphaVantageError):
    """The endpoint needs a paid plan (e.g. NEWS_SENTIMENT on a free key)."""


class QuotaExhaustedError(AlphaVantageError):
    """Daily request quota used up (ours or Alpha Vantage's); resume on the next run."""


class TransientError(AlphaVantageError):
    pass


def _utc_today() -> date:
    return datetime.now(UTC).date()


def classify(payload: Any, secret: str = "") -> None:
    """Raise if an HTTP-200 body is an Alpha Vantage error message.

    Alpha Vantage echoes the API key in some messages ("We have detected your API key as ..."),
    so `secret` is masked before the message goes anywhere."""
    if not isinstance(payload, dict):
        return
    msg = payload.get("Information") or payload.get("Note") or payload.get("Error Message")
    if msg is None or len(payload) > 1:
        return
    msg = str(msg).replace(secret, "***") if secret else str(msg)
    low = msg.lower()
    # Rate-limit messages also mention premium plans, so check them first.
    if "rate limit" in low or "requests per day" in low or "call frequency" in low:
        raise QuotaExhaustedError(msg)
    if "premium endpoint" in low or "premium" in low:
        raise PremiumRequiredError(msg)
    raise AlphaVantageError(msg)


class AlphaVantageClient:
    def __init__(
        self,
        api_key: str,
        storage: Storage,
        *,
        rate_limiter: TokenBucket | None = None,
        daily_quota: int | None = 25,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        wait: wait_base | None = None,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self._key = api_key
        self._storage = storage
        self._limiter = rate_limiter or TokenBucket(rate=5 / 60)
        self._quota = daily_quota
        self._http = http or httpx.Client(timeout=60)
        self._max_attempts = max_attempts
        self._wait = wait or wait_exponential_jitter(initial=2, max=60)
        self._today = today

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

    # -- transport -----------------------------------------------------------------------------

    def _get_once(self, params: dict[str, str]) -> bytes:
        if self._quota is not None and self.calls_today() >= self._quota:
            raise QuotaExhaustedError(f"daily quota of {self._quota} Alpha Vantage calls used")
        self._limiter.acquire()
        response = self._http.get(AV_URL, params={**params, "apikey": self._key})
        self._count_call()
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"HTTP {response.status_code}")
        response.raise_for_status()
        return response.content

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
        data: bytes = retrying(self._get_once, params)
        payload = json.loads(data)
        classify(payload, self._key)  # errors aren't landed, so they're retried next run
        if keep is None or keep(payload):
            self._storage.write_bytes(key, data)
        return payload

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


def public_url(params: dict[str, str]) -> str:
    """Request URL for lineage, without the API key."""
    return str(httpx.URL(AV_URL, params=params))


def content_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
