"""Phase 1 acceptance: `uw-pipeline run --all --stage company` -> 9 complete company rows."""

import logging

import pytest
import respx
from sqlalchemy import create_engine, select
from typer.testing import CliRunner

from tests.conftest import CONFIG_DIR, TEST_UA
from uw_pipeline.cli import app
from uw_pipeline.config import CompanyConfig
from uw_pipeline.extract.company import CompanyResolutionError, resolve_cik
from uw_pipeline.schema import company

REQUIRED = [
    "cik",
    "ticker",
    "name",
    "legal_name",
    "sic_code",
    "sic_description",
    "industry_group",
    "hq_city",
    "hq_state",
    "fiscal_year_end",
    "source_hash",
    "raw_path",
    "last_refreshed_at",
]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env from the repo
    monkeypatch.setenv("SEC_USER_AGENT", TEST_UA)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_DIR", str(CONFIG_DIR))
    db = tmp_path / "data" / "uw.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    return create_engine(f"sqlite:///{db}")


def _rows(engine):
    with engine.connect() as conn:
        return [r._asdict() for r in conn.execute(select(company).order_by(company.c.company_id))]


def test_run_all_company_stage(sec_routes: respx.MockRouter, cli_env) -> None:
    result = CliRunner().invoke(app, ["run", "--all", "--stage", "company"])
    assert result.exit_code == 0, result.output
    assert "SUCCEEDED: succeeded=9; dq=not run" in result.output  # no dq stage in this run
    rows = _rows(cli_env)
    assert len(rows) == 9
    for row in rows:
        missing = [c for c in REQUIRED if row[c] in (None, "")]
        assert not missing, f"{row['ticker']} missing {missing}"
    fi = {r["ticker"] for r in rows if r["is_financial"]}
    assert fi == {"JPM"}

    # Rerun: same rows, same surrogate keys, no duplicates.
    result = CliRunner().invoke(app, ["run", "--all", "--stage", "company", "--force"])
    assert result.exit_code == 0, result.output
    again = _rows(cli_env)
    assert [(r["company_id"], r["cik"]) for r in again] == [
        (r["company_id"], r["cik"]) for r in rows
    ]


def test_run_by_former_ticker(sec_routes: respx.MockRouter, cli_env) -> None:
    result = CliRunner().invoke(app, ["run", "--ticker", "PARA", "--stage", "company"])
    assert result.exit_code == 0, result.output
    assert [(r["ticker"], r["cik"]) for r in _rows(cli_env)] == [("SKYD", "0002041610")]


def test_unknown_ticker_rejected(cli_env) -> None:
    result = CliRunner().invoke(app, ["run", "--ticker", "ZZZZ"])
    assert result.exit_code != 0


def test_pinned_cik_beats_reassigned_ticker(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        cik = resolve_cik(CompanyConfig(ticker="PARA", cik=2041610), {"PARA": 1826011})
    assert cik == 2041610
    assert "pins 2041610" in caplog.text


def test_unpinned_unknown_ticker_raises() -> None:
    with pytest.raises(CompanyResolutionError):
        resolve_cik(CompanyConfig(ticker="ZZZZ"), {})
