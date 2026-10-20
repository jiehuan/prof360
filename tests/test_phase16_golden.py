"""Phase 16 (16e) golden values: one company per new or changed industry module, on recorded
SEC data. Every expected number is a fact of the company's latest 10-K or 10-Q (its XBRL, as
filed; USD millions unless noted); the tests check tag choice, formulas and fiscal periods."""

import pytest

from tests.test_phase2_golden import _golden_db, _metric


def _check(make_sec_client, ticker, period, expected):
    engine, run_all = _golden_db(make_sec_client, ticker)
    run_all()
    for code, value in expected.items():
        row = _metric(engine, code, *period)
        assert row["value"] == pytest.approx(value, rel=1e-9), code
    return engine


@pytest.mark.usefixtures("sec_routes")
def test_software_microsoft_fy2026(make_sec_client) -> None:
    """MSFT 10-K FY ended 2026-06-30: revenue 331,839, gross profit 225,465, R&D 35,562,
    operating income 155,237, depreciation 34,300 and intangible amortization 4,700 (tagged
    separately: EBITDA's second form), contract liabilities 72,965 (64,555 a year earlier)."""
    _check(make_sec_client, "MSFT", ("FY", 2026, 0), {
        "gross_margin": 225_465 / 331_839,
        "rnd_to_revenue": 35_562 / 331_839,
        "ebitda_computed": (155_237 + 34_300 + 4_700) * 1e6,
        "deferred_revenue": 72_965e6,
        "deferred_revenue_growth_yoy": 72_965 / 64_555 - 1,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_semiconductors_nvidia_fy2026(make_sec_client) -> None:
    """NVDA 10-K FY ended 2026-01-25: revenue 215,938, gross profit 153,463, cost of revenue
    62,475, R&D 18,497, inventory 21,403."""
    _check(make_sec_client, "NVDA", ("FY", 2026, 0), {
        "gross_margin": 153_463 / 215_938,
        "rnd_to_revenue": 18_497 / 215_938,
        "inventory_days": 21_403 / 62_475 * 365,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_hardware_cisco_fy2026(make_sec_client) -> None:
    """CSCO 10-K FY ended 2026-07-25: revenue 63,325, gross profit 40,860, R&D 9,563."""
    _check(make_sec_client, "CSCO", ("FY", 2026, 0), {
        "gross_margin": 40_860 / 63_325,
        "rnd_to_revenue": 9_563 / 63_325,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_medtech_medtronic_fy2026(make_sec_client) -> None:
    """MDT 10-K FY ended 2026-04-24: revenue 36,364 and cost of products sold 12,721 (no gross
    profit tag: the margin's second form)."""
    _check(make_sec_client, "MDT", ("FY", 2026, 0), {
        "gross_margin": (36_364 - 12_721) / 36_364,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_staples_coca_cola_fy2025(make_sec_client) -> None:
    """KO 10-K FY2025: revenue 47,941, gross profit 29,544."""
    _check(make_sec_client, "KO", ("FY", 2025, 0), {"gross_margin": 29_544 / 47_941})


@pytest.mark.usefixtures("sec_routes")
def test_utilities_duke_fy2025(make_sec_client) -> None:
    """DUK 10-K FY2025: operating revenues (regulated and unregulated) 32,237, regulatory
    assets (non-current) 14,379."""
    _check(make_sec_client, "DUK", ("FY", 2025, 0), {
        "revenue": 32_237e6,
        "regulatory_assets": 14_379e6,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_aerospace_lockheed_fy2025(make_sec_client) -> None:
    """LMT 10-K FY2025: non-current defined benefit pension liabilities 3,915."""
    _check(make_sec_client, "LMT", ("FY", 2025, 0), {"pension_liability": 3_915e6})


@pytest.mark.usefixtures("sec_routes")
def test_transport_union_pacific_fy2025(make_sec_client) -> None:
    """UNP 10-K FY2025: revenue 24,510, operating income 9,846 (operating ratio 59.8%)."""
    _check(make_sec_client, "UNP", ("FY", 2025, 0), {
        "operating_ratio": (24_510 - 9_846) / 24_510,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_payments_visa_fy2025(make_sec_client) -> None:
    """V 10-K FY ended 2025-09-30 (industry PAYMENTS by company setting): revenue 40,000,
    operating income 23,994."""
    _check(make_sec_client, "V", ("FY", 2025, 0), {
        "revenue": 40_000e6,
        "operating_income": 23_994e6,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_banks_bank_of_america_fy2025(make_sec_client) -> None:
    """BAC 10-K FY2025: revenue (us-gaap:Revenues, net of interest expense) 113,097, deposits
    2,018,729 (1,965,467 a year earlier), unrecognized losses on HTM securities 80,257 (no AFS
    gross loss tag), equity 303,243."""
    _check(make_sec_client, "BAC", ("FY", 2025, 0), {
        "revenue": 113_097e6,
        "deposits": 2_018_729e6,
        "deposit_growth_yoy": 2_018_729 / 1_965_467 - 1,
        "unrealized_loss_to_equity": 80_257 / 303_243,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_brokers_goldman_sachs_fy2025(make_sec_client) -> None:
    """GS 10-K FY2025: net revenues 58,283, net income 17,176, equity 124,972 (121,996 a year
    earlier), total assets 1,809,320."""
    _check(make_sec_client, "GS", ("FY", 2025, 0), {
        "revenue": 58_283e6,
        "roe": 17_176 / ((124_972 + 121_996) / 2),
        "assets_to_equity": 1_809_320 / 124_972,
    })  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
def test_retail_costco_12_12_12_16_week_quarters(make_sec_client) -> None:
    """COST fiscal 2026 (Q2 to 2026-02-15, Q3 to 2026-05-10, a 16-week Q4 to 2026-08-30): the
    10-Qs' 12-week quarters 69,597 and 70,527; 36 weeks 207,431; the year 303,154, so Q4 =
    95,723. Quarter ends come from the company's own 10-Qs (Phase 16)."""
    engine = _check(make_sec_client, "COST", ("Q", 2026, 2), {"revenue": 69_597e6})
    for fq, value in ((3, 70_527e6), (4, (303_154 - 207_431) * 1e6)):
        assert _metric(engine, "revenue", "Q", 2026, fq)["value"] == pytest.approx(value)


# Second company per module (PLAN 16e: two per industry group); values from each 10-K (USD
# millions).
SECOND = {
    # CRM FY ended 2026-01-31: revenue 41,525, gross profit 32,255, R&D 5,993.
    "CRM": (("FY", 2026, 0), {"gross_margin": 32_255 / 41_525, "rnd_to_revenue": 5_993 / 41_525}),
    # AMD FY2025: revenue 34,639, gross profit 17,152, R&D 8,091.
    "AMD": (("FY", 2025, 0), {"gross_margin": 17_152 / 34_639, "rnd_to_revenue": 8_091 / 34_639}),
    # AAPL FY ended 2025-09-27: revenue 416,161, gross profit 195,201, R&D 34,550.
    "AAPL": (("FY", 2025, 0), {"gross_margin": 195_201 / 416_161,
                               "rnd_to_revenue": 34_550 / 416_161}),
    # ISRG FY2025: revenue 10,064.7, gross profit 6,642.3, R&D 1,311.8.
    "ISRG": (("FY", 2025, 0), {"gross_margin": 6_642.3 / 10_064.7,
                               "rnd_to_revenue": 1_311.8 / 10_064.7}),
    # PG FY ended 2026-06-30: revenue 87,032, cost of products sold 43,362 (no gross profit tag).
    "PG": (("FY", 2026, 0), {"gross_margin": (87_032 - 43_362) / 87_032}),
    # NEE FY2025: operating revenues 27,412, regulatory assets (non-current) 5,639.
    "NEE": (("FY", 2025, 0), {"revenue": 27_412e6, "regulatory_assets": 5_639e6}),
    # BA FY2025: pension liabilities (non-current) 4,287, product warranty accrual 2,797.
    "BA": (("FY", 2025, 0), {"pension_liability": 4_287e6, "warranty_reserve": 2_797e6}),
    # UPS FY2025: revenue 88,661, operating income 7,867.
    "UPS": (("FY", 2025, 0), {"operating_ratio": (88_661 - 7_867) / 88_661}),
    # MA FY2025: net revenue 32,791, operating income 18,897.
    "MA": (("FY", 2025, 0), {"revenue": 32_791e6, "operating_income": 18_897e6}),
    # COF FY2025: total net revenue 53,434, deposits 475,771 (362,707 a year earlier).
    "COF": (("FY", 2025, 0), {"revenue": 53_434e6, "deposits": 475_771e6,
                              "deposit_growth_yoy": 475_771 / 362_707 - 1}),
    # MS FY2025: net revenues 70,645, net income 16,861, equity 111,632 (104,511 a year
    # earlier), total assets 1,420,270.
    "MS": (("FY", 2025, 0), {"revenue": 70_645e6,
                             "roe": 16_861 / ((111_632 + 104_511) / 2),
                             "assets_to_equity": 1_420_270 / 111_632}),
    # WMT FY ended 2026-01-31: revenue 713,163.
    "WMT": (("FY", 2026, 0), {"revenue": 713_163e6}),
}  # fmt: skip


@pytest.mark.usefixtures("sec_routes")
@pytest.mark.parametrize("ticker", sorted(SECOND))
def test_second_company_per_module(make_sec_client, ticker) -> None:
    period, expected = SECOND[ticker]
    _check(make_sec_client, ticker, period, expected)


@pytest.mark.usefixtures("sec_routes")
def test_share_metrics_use_stored_text_values(make_sec_client) -> None:
    """A formula over a text metric (top product's share of revenue) is computed from the value
    the text-metrics stage stored; a metric with XBRL and a text fallback (CET1) is not read
    back, so its XBRL values stay the formula's input (Phase 16)."""
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from uw_pipeline.jobs.run_xbrl import _stored_values
    from uw_pipeline.schema import metric_value

    engine, run_all = _golden_db(make_sec_client, "PFE")
    run_all()
    revenue = _metric(engine, "revenue", "FY", 2025, 0)
    base = dict(
        company_id=revenue["company_id"],
        period_type="FY",
        fiscal_year=2025,
        fiscal_quarter=0,
        period_end=revenue["period_end"],
        value_status="OK",
        derivation="LLM_EXTRACTED",
        formula="llm:10K-7",
        source_accn="a",
        source_tags='{"document": "10-K 2025 MD&A"}',
        computed_at=datetime.now(UTC),
    )
    with engine.begin() as conn:
        conn.execute(
            insert(metric_value).values(
                metric_code="top_product_revenue", value=5.0e9, unit="USD", **base
            )
        )
        conn.execute(
            insert(metric_value).values(
                metric_code="cet1_ratio", value=0.15, unit="fraction", **base
            )
        )
    run_all()
    share = _metric(engine, "top_product_share", "FY", 2025, 0)
    assert share["value"] == pytest.approx(5.0e9 / revenue["value"])
    with engine.connect() as conn:
        stored = _stored_values(conn, revenue["company_id"], {"top_product_revenue"})
    assert set(stored) == {"top_product_revenue"}
