import json
from datetime import UTC, datetime

from tests.conftest import CONFIG_DIR, SEC_FIXTURES
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.normalize.company import parse_submissions

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _parse(cik: str, cfg: CompanyConfig, payload: dict | None = None):
    if payload is None:
        payload = json.loads((SEC_FIXTURES / "submissions" / f"CIK{cik}.json").read_text())
    return parse_submissions(
        payload,
        cfg,
        load_industry_config(CONFIG_DIR),
        source_url="u",
        source_hash="h",
        raw_path="p",
        refreshed_at=NOW,
    )


def test_marriott() -> None:
    rec = _parse("0001048286", CompanyConfig(ticker="MAR", display_name="Marriott International"))
    assert rec.cik == "0001048286"
    assert rec.name == "Marriott International"
    assert rec.legal_name == "MARRIOTT INTERNATIONAL INC /MD/"
    assert (rec.sic_code, rec.sic_description) == ("7011", "Hotels & Motels")
    assert (rec.industry_group, rec.is_financial) == ("HOTELS", False)
    assert (rec.hq_city, rec.hq_state, rec.hq_country) == ("Bethesda", "MD", "US")
    assert rec.fiscal_year_end == "1231"
    assert rec.exchange == "Nasdaq"


def test_jpmorgan_is_financial() -> None:
    rec = _parse("0000019617", CompanyConfig(ticker="JPM"))
    assert (rec.industry_group, rec.is_financial) == ("BANKS", True)
    assert rec.name == rec.legal_name == "JPMORGAN CHASE & CO"  # no display_name override


def test_non_calendar_fiscal_year() -> None:
    assert _parse("0000794367", CompanyConfig(ticker="M")).fiscal_year_end == "0201"


def test_foreign_hq() -> None:
    payload = {
        "cik": "123",
        "name": "Foreign Co",
        "sic": "",
        "addresses": {
            "business": {
                "city": "TORONTO",
                "stateOrCountry": "A6",
                "stateOrCountryDescription": "Ontario, Canada",
                "isForeignLocation": 1,
            }
        },
    }
    rec = _parse("", CompanyConfig(ticker="FOO"), payload)
    assert (rec.hq_city, rec.hq_state, rec.hq_country) == ("Toronto", None, "Ontario, Canada")
    assert (rec.sic_code, rec.industry_group) == (None, "UNCLASSIFIED")
    assert rec.exchange is None
