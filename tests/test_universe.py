"""Company universe and development sample (PLAN 17a / 17a', D-055)."""

import json
from datetime import date

import httpx
import pytest
import respx

from tests.conftest import CONFIG_DIR, SEC_FIXTURES
from uw_pipeline import universe as uv
from uw_pipeline.clients.sec import COMPANY_TICKERS_EXCHANGE_URL, SEC_DATA
from uw_pipeline.config import CompanyConfig, load_industry_config

SINCE = date(2024, 7, 1)
UNIVERSE = SEC_FIXTURES / "universe"


def _summary(name: str = "Acme Corp", sic: int | None = 3711, **annual: str) -> dict:
    forms = {k.replace("_", "-"): v for k, v in annual.items()}
    return {"cik": 1, "name": name, "sic": sic, "sic_desc": "", "fye": "1231", "annual": forms}


def test_summary_keeps_the_latest_date_of_each_annual_form() -> None:
    payload = json.loads((SEC_FIXTURES / "submissions" / "CIK0001048286.json").read_text())
    s = uv.summarize(1048286, payload)
    assert (s["name"], s["sic"]) == ("MARRIOTT INTERNATIONAL INC /MD/", 7011)
    assert set(s["annual"]) == {"10-K"} and s["annual"]["10-K"] >= "2026-02-01"
    assert uv.scope(s, SINCE) == uv.IN_SCOPE


def test_scope_reasons() -> None:
    assert uv.scope(_summary(**{"10_K": "2026-02-10"}), SINCE) == uv.IN_SCOPE
    assert uv.scope(_summary(**{"10_KT": "2025-03-01"}), SINCE) == uv.IN_SCOPE
    assert uv.scope(_summary(**{"10_K": "2024-02-10"}), SINCE) == "no_annual_report"
    assert uv.scope(_summary(**{"20_F": "2026-04-01"}), SINCE) == "foreign_issuer"
    # The latest annual report decides: a foreign issuer that became a domestic filer is in.
    switched = _summary(**{"20_F": "2025-04-01", "10_K": "2026-03-01"})
    assert uv.scope(switched, SINCE) == uv.IN_SCOPE
    assert uv.scope(_summary(**{"N_CSR": "2026-03-01"}), SINCE) == "fund"
    assert uv.scope(_summary(sic=6770, **{"10_K": "2026-03-01"}), SINCE) == "spac"
    gold = _summary("SPDR GOLD TRUST", 6221, **{"10_K": "2026-05-01"})
    assert uv.scope(gold, SINCE) == "exchange_traded_product"
    eth = _summary("Bitwise Ethereum ETF", 6199, **{"10_K": "2026-03-01"})
    assert uv.scope(eth, SINCE) == "exchange_traded_product"
    royalty = _summary("Uranium Royalty Corp.", 6221, **{"10_K": "2026-07-01"})
    assert uv.scope(royalty, SINCE) == uv.IN_SCOPE  # an operating company under 6221
    northern = _summary("NORTHERN TRUST CORP", 6022, **{"10_K": "2026-02-20"})
    assert uv.scope(northern, SINCE) == uv.IN_SCOPE  # "Trust" outside the ETP codes


def test_listed_keeps_the_first_ticker_on_a_us_exchange() -> None:
    rows = [
        {"cik": 1, "ticker": "abc", "exchange": "NYSE"},
        {"cik": 1, "ticker": "ABC-PA", "exchange": "NYSE"},
        {"cik": 2, "ticker": "OTCX", "exchange": "OTC"},
        {"cik": 3, "ticker": "NEW", "exchange": None},
        {"cik": 4, "ticker": "CBX", "exchange": "CBOE"},
    ]
    assert uv.listed(rows) == {1: ("ABC", "NYSE"), 4: ("CBX", "CBOE")}


def test_revenue_prefers_the_newest_year_and_the_largest_revenue_tag() -> None:
    def frame(*rows: tuple[int, float]) -> dict:
        return {"data": [{"cik": c, "val": v} for c, v in rows]}

    frames = {
        ("Revenues", "CY2025"): frame((1, 100.0), (3, 7.0)),
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "CY2025"): frame((1, 90.0)),
        ("Revenues", "CY2024"): frame((1, 500.0), (2, 40.0)),
        ("InterestAndDividendIncomeOperating", "CY2025"): frame((3, 900.0), (4, 60.0)),
        ("NetIncomeLoss", "CY2025"): frame((5, 1.0)),  # not a revenue tag
    }
    rev = uv.revenue_by_cik(frames, ("CY2025", "CY2024"))
    assert rev[1] == uv.Revenue(100.0, "CY2025", "Revenues")  # newest year beats a bigger one
    assert rev[2].period == "CY2024"  # only the older year reported
    assert rev[3].tag == "Revenues"  # a revenue tag beats interest income
    assert rev[4].interest_income and rev[4].value == 60.0
    assert 5 not in rev


def test_revenue_tagged_1000_times_too_large_is_set_aside() -> None:
    """D-055: Tigo Energy tagged CY2025 contract revenue as $103.5B next to Revenues of
    $103.5M; a company's year jumping more than 20x is a scale error too."""

    def frame(*rows: tuple[int, float]) -> dict:
        return {"data": [{"cik": c, "val": v} for c, v in rows]}

    contract = "RevenueFromContractWithCustomerExcludingAssessedTax"
    frames = {
        ("Revenues", "CY2025"): frame((1, 103.5e6), (2, 49e9), (3, 60e9)),
        (contract, "CY2025"): frame((1, 103.5e9), (3, 2e9)),
        ("Revenues", "CY2024"): frame((2, 50e6)),
    }
    rev = uv.revenue_by_cik(frames, ("CY2025", "CY2024"))
    assert rev[1].value == 103.5e6 and "set aside" in rev[1].note
    assert (rev[2].value, rev[2].period) == (50e6, "CY2024") and "CY2025" in rev[2].note
    assert rev[3].value == 60e9 and not rev[3].note  # an insurer: total far above contract fees


def test_build_and_sample_rank_by_revenue_within_each_group() -> None:
    industry = load_industry_config(CONFIG_DIR)
    listing = {c: (f"T{c}", "NYSE") for c in range(1, 9)}
    summaries = {
        1: _summary("Big Hotels", 7011, **{"10_K": "2026-02-10"}),
        2: _summary("Small Hotels", 7011, **{"10_K": "2026-02-10"}),
        3: _summary("No Revenue Hotels", 7011, **{"10_K": "2026-02-10"}),
        4: _summary("Mid Hotels", 7011, **{"10_K": "2026-02-10"}),
        5: _summary("Shell Co", 9995, **{"10_K": "2026-02-10"}),  # no group
        6: _summary("Foreign Hotels", 7011, **{"20_F": "2026-04-10"}),
        7: _summary("Payments Inc", 7372, **{"10_K": "2026-02-10"}),
        8: _summary("Not Read", 7011, **{"10_K": "2026-02-10"}),
    }
    del summaries[8]  # a registrant whose submissions failed is left out
    revenue = {c: uv.Revenue(v, "CY2025", "Revenues") for c, v in
               {1: 900.0, 2: 10.0, 4: 50.0, 5: 1e6, 6: 1e6, 7: 5.0}.items()}  # fmt: skip
    configured = [CompanyConfig(ticker="T7", cik=7, industry="PAYMENTS", industry_reason="test")]
    entries = uv.build(listing, summaries, revenue, industry, configured, SINCE)
    assert len(entries) == 7
    by = {e.cik: e for e in entries}
    assert (by[7].group, by[7].how, by[7].configured) == ("PAYMENTS", "company", True)
    assert by[6].scope == "foreign_issuer" and by[6].group == ""
    chosen = uv.sample(entries, 3, industry.default_group)
    assert [e.cik for e in chosen["HOTELS"]] == [1, 4, 2]  # 3 has no revenue: last, cut
    assert [e.cik for e in chosen["PAYMENTS"]] == [7]
    assert "UNCLASSIFIED" not in chosen


def test_display_name_proposals() -> None:
    assert uv.display_name("MARRIOTT INTERNATIONAL INC /MD/") == "Marriott International"
    assert uv.display_name("Booking Holdings Inc. /DE/") == "Booking Holdings"
    assert uv.display_name("PROCTER & GAMBLE Co") == "Procter & Gamble"
    assert uv.display_name("NIKE, Inc.") == "Nike"  # capitals become title case
    assert uv.display_name("Chipotle Mexican Grill Inc") == "Chipotle Mexican Grill"


def test_sec_ticker_exchange_list_and_frames_are_landed(make_sec_client, tmp_path) -> None:
    frame_url = f"{SEC_DATA}/api/xbrl/frames/us-gaap/Revenues/USD/CY2025.json"
    with respx.mock(assert_all_called=True) as router:
        router.get(COMPANY_TICKERS_EXCHANGE_URL).mock(return_value=httpx.Response(
            200, content=(UNIVERSE / "company_tickers_exchange.json").read_bytes()))  # fmt: skip
        router.get(frame_url).mock(return_value=httpx.Response(
            200, content=(UNIVERSE / "frames_Revenues_USD_CY2025.json").read_bytes()))  # fmt: skip
        sec = make_sec_client()
        rows = sec.company_tickers_exchange()
        frame = sec.frame("Revenues", "USD", "CY2025")
    listing = uv.listed(rows)
    assert listing[1048286] == ("MAR", "Nasdaq")
    assert listing[19617] == ("JPM", "NYSE")  # common stock, not the preferred JPM-PC
    assert all(ex in uv.EXCHANGES for _, ex in listing.values())
    assert frame.raw_path == "sec/frames/us-gaap/Revenues/USD/CY2025/2026-10-13.json"
    assert (tmp_path / "raw" / frame.raw_path).is_file()
    rev = uv.revenue_by_cik({("Revenues", "CY2025"): frame.payload}, ("CY2025",))
    assert rev[1048286].value > 20e9  # Marriott's 2025 revenue


def test_unlanded_submissions_are_not_stored(sec_routes, make_sec_client, tmp_path) -> None:
    doc = make_sec_client().submissions(1048286, land=False)
    assert doc.raw_path == "" and doc.payload["name"].startswith("MARRIOTT")
    assert not (tmp_path / "raw" / "sec" / "submissions").exists()


def test_universe_review_settings_reclassify_and_skip() -> None:
    industry = load_industry_config(CONFIG_DIR)
    universe = uv.UniverseConfig.model_validate({
        "industry": [{"cik": 1, "ticker": "CZR", "industry": "LEISURE_GAMING", "reason": "casinos"},
                     {"cik": 2, "ticker": "CFG", "industry": "BANKS", "reason": "ignored"}],
        "sample_skip": [{"cik": 3, "ticker": "SNEX", "reason": "gross revenue"}],
    })  # fmt: skip
    universe.check(industry)
    listing = {c: (f"T{c}", "NYSE") for c in (1, 2, 3)}
    summaries = {c: _summary(f"Co {c}", 7011, **{"10_K": "2026-02-10"}) for c in (1, 2, 3)}
    revenue = {c: uv.Revenue(float(c), "CY2025", "Revenues") for c in (1, 2, 3)}
    configured = [CompanyConfig(ticker="T2", cik=2)]  # companies.yaml wins over universe.yaml
    entries = uv.build(listing, summaries, revenue, industry, configured, SINCE, universe)
    by = {e.cik: e for e in entries}
    assert (by[1].group, by[1].how, by[1].reason) == ("LEISURE_GAMING", "universe", "casinos")
    assert (by[2].group, by[2].how) == ("HOTELS", "sic")
    chosen = uv.sample(entries, 5, industry.default_group, [s.cik for s in universe.sample_skip])
    assert [e.cik for e in chosen["HOTELS"]] == [2]


def test_the_repository_universe_settings_are_valid() -> None:
    industry = load_industry_config(CONFIG_DIR)
    universe = uv.load_universe_config(CONFIG_DIR)
    universe.check(industry)
    assert {r.ticker for r in universe.industry} >= {"MGM", "LVS", "NKE", "KLAC", "PYPL"}
    bad = uv.UniverseConfig.model_validate(
        {"industry": [{"cik": 1, "ticker": "X", "industry": "NOPE", "reason": "r"}]})  # fmt: skip
    with pytest.raises(ValueError, match="NOPE"):
        bad.check(industry)


def test_clinical_candidates_are_pre_revenue_life_sciences() -> None:
    """D-058: life-sciences SIC codes with revenue under $10M or none tagged."""
    industry = load_industry_config(CONFIG_DIR)
    listing = {c: (f"T{c}", "Nasdaq") for c in range(1, 6)}
    summaries = {
        1: _summary("Pre Revenue Bio", 2834, **{"10_K": "2026-03-01"}),
        2: _summary("Small Revenue Bio", 2836, **{"10_K": "2026-03-01"}),
        3: _summary("Big Pharma", 2834, **{"10_K": "2026-03-01"}),
        4: _summary("Software Startup", 7372, **{"10_K": "2026-03-01"}),
        5: _summary("Device Startup", 3841, **{"20_F": "2026-03-01"}),  # foreign: out of scope
    }
    revenue = {2: uv.Revenue(4e6, "CY2025", "Revenues"), 3: uv.Revenue(60e9, "CY2025", "Revenues")}
    entries = uv.build(listing, summaries, revenue, industry, [], SINCE)
    assert [e.cik for e in uv.clinical_candidates(entries)] == [1, 2]
    frames = {"CY2025": {"data": [{"cik": 1, "val": 300e6}]},
              "CY2024": {"data": [{"cik": 1, "val": 200e6}, {"cik": 2, "val": 50e6}]}}  # fmt: skip
    assert uv.latest_by_cik(frames, ("CY2025", "CY2024")) == {1: 300e6, 2: 50e6}
