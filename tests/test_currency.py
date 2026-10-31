"""D-072: a company that reports in another currency (Canopy Growth, CAD)."""

import pandas as pd
import pytest

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_industry_config
from uw_pipeline.enrich.package import fmt_value
from uw_pipeline.normalize.periods import learn_calendar, standardize


def _fact(tag, start, end, val, unit, form="10-Q", fp="Q1", fy=2026, accn="q1"):
    return {
        "taxonomy": "us-gaap", "tag": tag, "dims": "", "unit": unit, "val": float(val),
        "start": start, "end": end, "fy": fy, "fp": fp, "form": form, "accn": accn,
        "filed": "2026-08-07", "frame": None, "source": "companyfacts",
    }  # fmt: skip


def _facts(unit):
    rev = "RevenueFromContractWithCustomerExcludingAssessedTax"
    return pd.DataFrame([
        _fact(rev, "2025-04-01", "2025-06-30", 60, unit, fp="Q1", accn="q1"),
        _fact(rev, "2025-04-01", "2025-09-30", 130, unit, fp="Q2", accn="q2"),
        _fact(rev, "2025-04-01", "2025-12-31", 200, unit, fp="Q3", accn="q3"),
        _fact(rev, "2025-04-01", "2026-03-31", 270, unit, form="10-K", fp="FY", accn="k"),
        _fact("AssetsCurrent", None, "2026-03-31", 500, unit, form="10-K", fp="FY", accn="k"),
        _fact("LiabilitiesCurrent", None, "2026-03-31", 250, unit, form="10-K", fp="FY", accn="k"),
    ])  # fmt: skip


def test_money_is_read_and_stored_in_the_companys_currency() -> None:
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    creg = reg.for_company("CGC", ind.groups["PHARMA"])
    assert creg.currency == "CAD" and creg.units["revenue"] == "CAD"
    revenue = next(m for m in creg.metrics if m.code == "revenue")
    ratio = next(m for m in creg.metrics if m.code == "current_ratio")
    assert creg.unit_of(revenue) == "CAD" and creg.unit_of(ratio) == "ratio"
    # Market cap is in USD (the share price): nothing combines it with CAD amounts.
    assert not any("market_cap" in m.inputs() for m in creg.metrics)

    facts = _facts("CAD")
    out = compute_metrics(standardize(facts, learn_calendar(facts, "0331")), creg)
    assert out["revenue"][("FY", 2026, 0)].value == 270
    assert out["revenue"][("Q", 2026, 4)].value == pytest.approx(70)  # FY - 9 months, in CAD
    assert out["current_ratio"][("FY", 2026, 0)].value == pytest.approx(2.0)
    # USD facts are not read for a CAD company.
    usd = _facts("USD")
    assert not compute_metrics(standardize(usd, learn_calendar(usd, "0331")), creg).get("revenue")


def test_other_companies_stay_in_usd() -> None:
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    creg = reg.for_company("PFE", ind.groups["PHARMA"])
    assert creg.currency == "USD" and creg.units["revenue"] == "USD"
    assert any(m.code == "altman_z" for m in creg.metrics)


def test_amounts_show_their_currency() -> None:
    assert fmt_value(1.234e9, "CAD") == "C$1.23B"
    assert fmt_value(5.6e6, "USD") == "$6M"
    assert fmt_value(12.5, "EUR") == "EUR 12.50"
    assert fmt_value(0.05, "fraction") == "5.0%"
