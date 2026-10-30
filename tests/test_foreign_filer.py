"""D-071: a foreign filer (20-F / 40-F) reports XBRL under IFRS, with no US-GAAP facts at all;
the facts stage must still succeed, with no facts, so the later stages run."""

from datetime import date
from types import SimpleNamespace

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.jobs.run_xbrl import load_company_facts

IFRS_ONLY = {
    "cik": 1833835,
    "entityName": "Paysafe Ltd",
    "facts": {"ifrs-full": {"Revenue": {"units": {"USD": [
        {"end": "2025-12-31", "val": 1.7e9, "accn": "0001833835-26-000005", "fy": 2025,
         "fp": "FY", "form": "20-F", "filed": "2026-03-03"},
    ]}}}},
}  # fmt: skip
SUBMISSIONS = {
    "cik": "1833835",
    "filings": {"recent": {k: [] for k in ("accessionNumber", "form", "filingDate", "reportDate",
                                           "primaryDocument", "items", "isXBRL")},
                "files": []},
}  # fmt: skip


class StubSec:
    def companyfacts(self, cik, force=False):
        return SimpleNamespace(payload=IFRS_ONLY, content_hash="h")

    def submissions(self, cik, force=False, land=True):
        return SimpleNamespace(payload=SUBMISSIONS)


def test_no_us_gaap_facts_gives_an_empty_table() -> None:
    industry = load_industry_config(CONFIG_DIR)
    cfg = CompanyConfig(ticker="PSFE", cik=1833835)
    group, _ = industry.classify(7389, cfg)
    creg = load_registry(CONFIG_DIR).for_company("PSFE", group)
    facts, digest = load_company_facts(
        cfg, StubSec(), creg, cik="0001833835", today=date(2026, 10, 30), history_years=6,
        force=False,
    )  # fmt: skip
    assert facts.empty and digest
    assert {"tag", "val", "end"} <= set(facts.columns)
