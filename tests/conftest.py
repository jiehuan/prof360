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
from uw_pipeline.clients.sec import COMPANY_TICKERS_URL, SEC_DATA, SecClient, archive_folder_url
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
        for path in (SEC_FIXTURES / "companyfacts").glob("CIK*.json"):
            router.get(f"{SEC_DATA}/api/xbrl/companyfacts/{path.name}").mock(
                return_value=httpx.Response(200, content=path.read_bytes())
            )
        for path in (SEC_FIXTURES / "filings").glob("CIK*/*/*"):
            cik, accn = path.parent.parent.name.removeprefix("CIK"), path.parent.name
            router.get(f"{archive_folder_url(cik, accn)}/{path.name}").mock(
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


# -- the whole suite on SQL Server (Phase 12) ------------------------------------------------
# With UW_TEST_ON_MSSQL=1 and MSSQL_TEST_URL set (scripts/test_mssql.sh --all), every
# `create_engine(...)` in a test module returns a SQL Server engine with a fresh schema, so
# the unit tests run unchanged against SQL Server. Tables are dropped after each test.


@pytest.fixture(autouse=True)
def _tests_on_mssql(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    import os

    url = os.environ.get("MSSQL_TEST_URL")
    if not (url and os.environ.get("UW_TEST_ON_MSSQL")) or not hasattr(
        request.module, "create_engine"
    ):
        yield
        return
    from sqlalchemy import Engine, inspect, text

    from uw_pipeline.clients.db import make_engine

    made: list[tuple[Engine, str]] = []

    real_create_engine = request.module.create_engine

    def factory(*args: Any, **kwargs: Any) -> Engine:
        if args and str(args[0]).startswith("sqlite:///"):
            # A file database shared with the CLI under test: keep it (CLI wiring, not SQL).
            return real_create_engine(*args, **kwargs)  # type: ignore[no-any-return]
        import uuid

        schema = f"t_{uuid.uuid4().hex[:12]}"
        engine = make_engine(url, schema)
        with engine.begin() as c:
            c.execute(text(f"CREATE SCHEMA [{schema}]"))
        made.append((engine, schema))
        return engine

    monkeypatch.setattr(request.module, "create_engine", factory)
    yield
    for engine, schema in made:
        with engine.begin() as c:
            # Drop whatever the test created: foreign keys first, then tables. (Table names are
            # case-insensitive on SQL Server, so app "Company" and pipeline "company" are one.)
            sql = ("SELECT OBJECT_NAME(parent_object_id), name FROM sys.foreign_keys "
                   "WHERE OBJECT_SCHEMA_NAME(parent_object_id) = :s")  # fmt: skip
            fks = c.execute(text(sql), {"s": schema}).all()
            for table, fk in fks:
                c.execute(text(f"ALTER TABLE [{schema}].[{table}] DROP CONSTRAINT [{fk}]"))
            for table in inspect(c).get_table_names(schema=schema):
                c.execute(text(f"DROP TABLE [{schema}].[{table}]"))
            c.execute(text(f"DROP SCHEMA [{schema}]"))
        engine.dispose()
