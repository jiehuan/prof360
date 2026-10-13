"""SEC EDGAR client: User-Agent, global rate limit, retries, and raw landing/caching.

Every response is written to storage as-is before parsing, keyed by fetch date, so runs are
replayable and same-day reruns do not hit EDGAR again (unless `force=True`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.storage import Storage

log = logging.getLogger(__name__)

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_WWW}/files/company_tickers.json"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableHTTPError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} for {response.request.url}")
        self.response = response


def pad_cik(cik: int | str) -> str:
    """Zero-pad a CIK to the 10 digits EDGAR uses in URLs and identifiers."""
    s = str(cik).strip()
    s = s.removeprefix("CIK")
    if not s.isdigit() or len(s) > 10:
        raise ValueError(f"invalid CIK: {cik!r}")
    return s.zfill(10)


def _utc_today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class RawDocument:
    url: str
    raw_path: str  # storage key, relative to the raw root
    content_hash: str
    from_cache: bool
    payload: Any  # parsed JSON, or raw bytes for archive documents


class SecClient:
    def __init__(
        self,
        user_agent: str,
        storage: Storage,
        *,
        rate_limiter: TokenBucket | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        max_attempts: int = 5,
        wait: wait_base | None = None,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._storage = storage
        self._limiter = rate_limiter or TokenBucket(rate=8.0)
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self._max_attempts = max_attempts
        self._wait = wait or wait_exponential_jitter(initial=1, max=30)
        self._today = today

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------------------------

    def _get_once(self, url: str) -> bytes:
        self._limiter.acquire()
        response = self._http.get(url, headers=self._headers)
        if response.status_code in RETRYABLE_STATUS:
            log.warning("SEC %s returned %s; will retry", url, response.status_code)
            raise RetryableHTTPError(response)
        response.raise_for_status()
        return response.content

    def get_bytes(self, url: str) -> bytes:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((httpx.TransportError, RetryableHTTPError)),
            reraise=True,
        )
        result: bytes = retrying(self._get_once, url)
        return result

    def _fetch(self, url: str, key: str, *, force: bool) -> tuple[bytes, bool]:
        from_cache = not force and self._storage.exists(key)
        if from_cache:
            data = self._storage.read_bytes(key)
        else:
            data = self.get_bytes(url)
            self._storage.write_bytes(key, data)
        return data, from_cache

    def _fetch_json(self, url: str, key_prefix: str, *, force: bool) -> RawDocument:
        key = f"{key_prefix}/{self._today().isoformat()}.json"
        data, from_cache = self._fetch(url, key, force=force)
        return RawDocument(
            url=url,
            raw_path=key,
            content_hash=hashlib.sha256(data).hexdigest(),
            from_cache=from_cache,
            payload=json.loads(data),
        )

    # -- endpoints ---------------------------------------------------------------------------

    def company_tickers(self, *, force: bool = False) -> dict[str, int]:
        """Map upper-case ticker -> CIK from SEC's company_tickers.json."""
        doc = self._fetch_json(COMPANY_TICKERS_URL, "sec/company_tickers", force=force)
        return {row["ticker"].upper(): int(row["cik_str"]) for row in doc.payload.values()}

    def submissions(self, cik: int | str, *, force: bool = False) -> RawDocument:
        padded = pad_cik(cik)
        url = f"{SEC_DATA}/submissions/CIK{padded}.json"
        return self._fetch_json(url, f"sec/submissions/CIK{padded}", force=force)

    def submissions_page(self, cik: int | str, name: str, *, force: bool = False) -> RawDocument:
        """Older filings beyond `filings.recent`, e.g. CIK0000019617-submissions-001.json."""
        url = f"{SEC_DATA}/submissions/{name}"
        stem = name.removesuffix(".json")
        return self._fetch_json(url, f"sec/submissions/CIK{pad_cik(cik)}/pages/{stem}", force=force)

    def companyfacts(self, cik: int | str, *, force: bool = False) -> RawDocument:
        padded = pad_cik(cik)
        url = f"{SEC_DATA}/api/xbrl/companyfacts/CIK{padded}.json"
        return self._fetch_json(url, f"sec/companyfacts/CIK{padded}", force=force)

    # Archive documents are immutable once filed, so they are cached by accession, not by date.

    def filing_index(self, cik: int | str, accession_no: str) -> list[str]:
        """File names in a filing folder."""
        url = f"{archive_folder_url(cik, accession_no)}/index.json"
        key = f"sec/filings/CIK{pad_cik(cik)}/{accession_no}/index.json"
        data, _ = self._fetch(url, key, force=False)
        return [item["name"] for item in json.loads(data)["directory"]["item"]]

    def filing_document(self, cik: int | str, accession_no: str, name: str) -> RawDocument:
        url = f"{archive_folder_url(cik, accession_no)}/{name}"
        key = f"sec/filings/CIK{pad_cik(cik)}/{accession_no}/{name}"
        data, from_cache = self._fetch(url, key, force=False)
        return RawDocument(
            url=url,
            raw_path=key,
            content_hash=hashlib.sha256(data).hexdigest(),
            from_cache=from_cache,
            payload=data,
        )


def archive_folder_url(cik: int | str, accession_no: str) -> str:
    return f"{SEC_WWW}/Archives/edgar/data/{int(pad_cik(cik))}/{accession_no.replace('-', '')}"
