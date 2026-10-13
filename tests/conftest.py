from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tenacity import wait_none

from uw_pipeline.clients.ratelimit import TokenBucket
from uw_pipeline.clients.sec import COMPANY_TICKERS_URL, SEC_DATA, SecClient
from uw_pipeline.clients.storage import LocalStorage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SEC_FIXTURES = FIXTURES / "sec"
CONFIG_DIR = ROOT / "config"
TEST_UA = "Test Runner test@example.com"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must never reach the network; respx mocks sit above the socket layer."""

    def guard(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("network access attempted in a unit test")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def sec_routes() -> Iterator[respx.MockRouter]:
    """Serve recorded SEC responses; any unmocked URL fails the test."""
    with respx.mock(assert_all_called=False) as router:
        router.get(COMPANY_TICKERS_URL).mock(
            return_value=httpx.Response(
                200, content=(SEC_FIXTURES / "company_tickers.json").read_bytes()
            )
        )
        for path in (SEC_FIXTURES / "submissions").glob("CIK*.json"):
            router.get(f"{SEC_DATA}/submissions/{path.name}").mock(
                return_value=httpx.Response(200, content=path.read_bytes())
            )
        yield router


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def make_sec_client(tmp_path: Path) -> Iterator[Any]:
    clients: list[SecClient] = []

    def factory(*, day: date = date(2026, 10, 13), max_attempts: int = 3) -> SecClient:
        clock = FakeClock()
        client = SecClient(
            TEST_UA,
            LocalStorage(tmp_path / "raw"),
            rate_limiter=TokenBucket(rate=8, clock=clock, sleep=clock.sleep),
            max_attempts=max_attempts,
            wait=wait_none(),
            today=lambda: day,
        )
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.close()
