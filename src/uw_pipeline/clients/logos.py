"""Logo sources (D-067): Wikidata, Wikimedia Commons, English Wikipedia and Google's favicon
service.

Used only by scripts/fetch_logos.py, on a network that reaches these hosts (the corporate one
does not); the pipeline reads the files it writes under config/logos/. Wikimedia asks for a
descriptive User-Agent with contact details; the SEC one is reused.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import wait_base

from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.sec import RETRYABLE_STATUS, RetryableHTTPError

log = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ENWIKI_API = "https://en.wikipedia.org/w/api.php"  # also serves Commons files
FAVICON_URL = "https://www.google.com/s2/favicons"


class LogoClient:
    def __init__(
        self,
        user_agent: str,
        *,
        rate_limiter: TokenBucket | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        wait: wait_base | None = None,
    ) -> None:
        self._headers = {"User-Agent": user_agent}
        self._limiter = rate_limiter or TokenBucket(rate=2.0)
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self._max_attempts = max_attempts
        self._wait = wait or wait_exponential_jitter(initial=1, max=20)
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def _get_once(self, url: str, params: dict[str, Any] | None) -> httpx.Response | None:
        self._limiter.acquire()
        self.requests += 1
        response = self._http.get(url, params=params, headers=self._headers)
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableHTTPError(response)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response | None:
        """The response, or None for 404."""
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((httpx.TransportError, RetryableHTTPError)),
            reraise=True,
        )
        result: httpx.Response | None = retrying(self._get_once, url, params)
        return result

    def sparql(self, query: str) -> list[dict[str, str]]:
        """Rows of a Wikidata SPARQL query, each a dict of variable -> value."""
        response = self._get(WIKIDATA_SPARQL, {"query": query, "format": "json"})
        if response is None:
            return []
        return [{k: v["value"] for k, v in b.items()}
                for b in response.json()["results"]["bindings"]]  # fmt: skip

    def thumbnail(self, api: str, file_name: str, width: int) -> tuple[str, str] | None:
        """(thumbnail URL, file page URL) of a file of the wiki at `api`, `width` pixels wide;
        SVG files come as PNG. None when the file does not exist."""
        response = self._get(api, {
            "action": "query", "titles": f"File:{file_name}", "prop": "imageinfo",
            "iiprop": "url", "iiurlwidth": width, "format": "json",
        })  # fmt: skip
        if response is None:
            return None
        for page in response.json().get("query", {}).get("pages", {}).values():
            for info in page.get("imageinfo", []):
                thumb = info.get("thumburl") or info.get("url")
                if thumb:
                    return thumb, info.get("descriptionurl") or thumb
        return None

    def wikitext(self, title: str) -> str | None:
        """The lead section's wikitext of an English Wikipedia article (redirects followed)."""
        response = self._get(ENWIKI_API, {
            "action": "parse", "page": title, "prop": "wikitext", "section": 0,
            "redirects": 1, "format": "json", "formatversion": 2,
        })  # fmt: skip
        if response is None:
            return None
        text: str | None = response.json().get("parse", {}).get("wikitext")
        return text

    def image(self, url: str) -> bytes | None:
        response = self._get(url)
        return response.content if response is not None else None

    def favicon(self, host: str, size: int) -> bytes | None:
        """A site's icon from Google's favicon service (None when it has none: 404)."""
        response = self._get(FAVICON_URL, {"domain": host, "sz": size})
        return response.content if response is not None else None
