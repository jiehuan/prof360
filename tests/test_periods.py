from datetime import date

import pandas as pd

from uw_pipeline.normalize.periods import FiscalCalendar, learn_calendar, standardize
from uw_pipeline.normalize.xbrl_facts import FACT_COLUMNS


def fact(tag, start, end, val, *, filed="2026-01-01", accn="a1", form="10-Q", fy=None, fp=None):
    return {
        "taxonomy": "us-gaap", "tag": tag, "dims": "", "unit": "USD", "val": float(val),
        "start": start, "end": end, "fy": fy, "fp": fp, "form": form, "accn": accn,
        "filed": filed, "frame": None, "source": "companyfacts",
    }  # fmt: skip


def frame(*rows):
    return pd.DataFrame(list(rows), columns=FACT_COLUMNS)


def values(result, period_type):
    sel = result[result["period_type"] == period_type]
    return {(r.fiscal_year, r.fiscal_quarter): (r.value, r.derivation) for r in sel.itertuples()}


DEC = FiscalCalendar("1231")


def test_locate_calendar_and_52_53_week_ends() -> None:
    assert DEC.locate(date(2026, 6, 30)) == (2026, 2)
    assert DEC.locate(date(2026, 6, 28)) == (2026, 2)  # Pfizer-style 52/53-week quarter
    assert DEC.locate(date(2025, 12, 31)) == (2025, 4)
    assert DEC.locate(date(2026, 5, 15)) is None  # not near a quarter end
    macys = FiscalCalendar("0201", year_offset=-1)
    assert macys.locate(date(2026, 1, 31)) == (2025, 4)
    assert macys.locate(date(2026, 8, 1)) == (2026, 2)


def test_learn_calendar_offset_from_10k() -> None:
    facts = frame(
        fact("Revenues", "2025-02-02", "2026-01-31", 100, form="10-K", fy=2025, fp="FY"),
        fact("Revenues", "2024-02-04", "2025-02-01", 90, form="10-K", fy=2025, fp="FY"),
    )
    assert learn_calendar(facts, "0201") == FiscalCalendar("0201", -1)


def test_q4_and_cash_flow_quarters_are_derived_from_ytd() -> None:
    facts = frame(
        # income statement: direct quarters for Q1-Q3, FY from the 10-K
        fact("Revenues", "2025-01-01", "2025-03-31", 10),
        fact("Revenues", "2025-04-01", "2025-06-30", 20),
        fact("Revenues", "2025-07-01", "2025-09-30", 30),
        fact("Revenues", "2025-01-01", "2025-09-30", 60),
        fact("Revenues", "2025-01-01", "2025-12-31", 100, form="10-K"),
        # cash flow: YTD only
        fact("NetCash", "2025-01-01", "2025-03-31", 5),
        fact("NetCash", "2025-01-01", "2025-06-30", 12),
        fact("NetCash", "2025-01-01", "2025-09-30", 20),
        fact("NetCash", "2025-01-01", "2025-12-31", 31, form="10-K"),
    )
    out = standardize(facts, DEC)
    rev = values(out[out["tag"] == "Revenues"], "Q")
    assert rev == {
        (2025, 1): (10, "REPORTED"),
        (2025, 2): (20, "REPORTED"),
        (2025, 3): (30, "REPORTED"),
        (2025, 4): (40, "DERIVED"),  # FY - 9M
    }
    cf = values(out[out["tag"] == "NetCash"], "Q")
    assert {k: v for k, (v, _) in cf.items()} == {
        (2025, 1): 5, (2025, 2): 7, (2025, 3): 8, (2025, 4): 11,
    }  # fmt: skip
    assert values(out[out["tag"] == "Revenues"], "FY") == {(2025, 0): (100, "REPORTED")}


def test_share_counts_are_not_derived() -> None:
    """A weighted-average share count is not additive: no Q4 = FY - 9M, no quarters from YTD
    (Phase 15c: Southwest's Q4 came out as 0 and 7M shares before this)."""
    shares = [
        fact("Diluted", "2025-01-01", "2025-03-31", 600),
        fact("Diluted", "2025-04-01", "2025-06-30", 590),
        fact("Diluted", "2025-01-01", "2025-06-30", 595),
        fact("Diluted", "2025-07-01", "2025-09-30", 580),
        fact("Diluted", "2025-01-01", "2025-09-30", 590),
        fact("Diluted", "2025-01-01", "2025-12-31", 585, form="10-K"),
    ]
    out = standardize(frame(*[r | {"unit": "shares"} for r in shares]), DEC)
    assert values(out, "Q") == {(2025, 1): (600, "REPORTED"), (2025, 2): (590, "REPORTED"),
                                (2025, 3): (580, "REPORTED")}  # fmt: skip
    assert values(out, "FY") == {(2025, 0): (585, "REPORTED")}
    assert values(out, "YTD") == {}


def test_q4_from_quarter_sum_when_9m_missing() -> None:
    facts = frame(
        fact("Revenues", "2025-01-01", "2025-03-31", 10),
        fact("Revenues", "2025-04-01", "2025-06-30", 20),
        fact("Revenues", "2025-07-01", "2025-09-30", 30),
        fact("Revenues", "2025-01-01", "2025-12-31", 100, form="10-K"),
    )
    assert values(standardize(facts, DEC), "Q")[(2025, 4)] == (40, "DERIVED")


def test_restatement_latest_filing_wins() -> None:
    facts = frame(
        fact("Revenues", "2025-01-01", "2025-03-31", 10, filed="2025-05-01", accn="q1"),
        fact("Revenues", "2025-01-01", "2025-03-31", 12, filed="2026-05-01", accn="q1-2026"),
    )
    out = standardize(facts, DEC)
    assert values(out, "Q") == {(2025, 1): (12, "REPORTED")}
    assert out.iloc[0]["accn"] == "q1-2026"


def test_a_quarter_restated_after_its_year_keeps_the_years_basis() -> None:
    """LUV capex (D-035, D-046): the 2026 10-Qs restate 2025 Q1 while FY2025 stays as reported.
    Q1 keeps the basis of the year, so the quarters still add up to it; the next 10-K, which
    restates the year, moves the whole year to the new basis."""
    q1 = fact("Capex", "2025-01-01", "2025-03-31", 501, filed="2025-04-24", accn="q1")
    q2 = fact("Capex", "2025-01-01", "2025-06-30", 1136, filed="2025-07-24", accn="q2")  # YTD
    q3 = fact("Capex", "2025-01-01", "2025-09-30", 1800, filed="2025-10-23", accn="q3")  # YTD
    fy = fact("Capex", "2025-01-01", "2025-12-31", 2673, form="10-K", filed="2026-02-05",
              accn="k")  # fmt: skip
    restated = fact("Capex", "2025-01-01", "2025-03-31", 527, filed="2026-04-24", accn="q1-26")
    found: list[str] = []
    out = values(standardize(frame(q1, q2, q3, fy, restated), DEC, found), "Q")
    assert out[(2025, 1)] == (501, "REPORTED")
    assert sum(v for (fy_, _), (v, _) in out.items() if fy_ == 2025) == 2673
    assert found and "527" in found[0]
    # The next 10-K restates FY2025 (comparative): the restated quarter now matches its year.
    fy_new = fact("Capex", "2025-01-01", "2025-12-31", 2699, form="10-K", filed="2027-02-04",
                  accn="k2")  # fmt: skip
    out = values(standardize(frame(q1, q2, q3, fy, restated, fy_new), DEC), "Q")
    assert out[(2025, 1)] == (527, "REPORTED")


def test_instants_map_to_quarter_and_fy() -> None:
    facts = frame(
        fact("Cash", None, "2025-09-30", 5),
        fact("Cash", None, "2025-12-31", 7, form="10-K"),
        fact("Cash", None, "2025-11-15", 99),  # not a period end
    )
    out = standardize(facts, DEC)
    assert values(out, "Q") == {(2025, 3): (5, "REPORTED"), (2025, 4): (7, "REPORTED")}
    assert values(out, "FY") == {(2025, 0): (7, "REPORTED")}


def test_off_calendar_durations_are_ignored() -> None:
    facts = frame(
        fact("Revenues", "2025-08-08", "2025-12-31", 50, form="10-K"),  # merger stub period
        fact("Revenues", "2025-04-01", "2025-09-30", 50),  # 6 months not starting at FY start
    )
    assert standardize(facts, DEC).empty


def test_a_year_whose_10k_changed_the_basis() -> None:
    """BAC revenue (D-052): the FY2025 10-K recast FY2024 (101,887 -> 105,856), and the 2026
    10-Qs restated the 2025 quarters. The restated quarters match their year and are kept; a
    fourth quarter whose year and nine months are on different bases is N/M, not a value that
    carries the whole restatement (it was 31,180 and 29,316 instead of about 28,400 and 26,500)."""

    def k(start, end, val, filed, accn):
        return fact("Revenues", start, end, val, form="10-K", filed=filed, accn=accn)

    rows = [
        fact("Revenues", "2024-01-01", "2024-09-30", 76_540, filed="2024-10-30", accn="q3-24"),
        k("2024-01-01", "2024-12-31", 101_887, "2025-02-25", "k24"),
        fact("Revenues", "2025-01-01", "2025-03-31", 27_366, filed="2025-04-29", accn="q1-25"),
        fact("Revenues", "2025-04-01", "2025-06-30", 26_463, filed="2025-07-29", accn="q2-25"),
        fact("Revenues", "2025-07-01", "2025-09-30", 28_088, filed="2025-10-30", accn="q3-25"),
        fact("Revenues", "2025-01-01", "2025-09-30", 81_917, filed="2025-10-30", accn="q3-25"),
        k("2024-01-01", "2024-12-31", 105_856, "2026-02-24", "k25"),  # recast comparative
        k("2025-01-01", "2025-12-31", 113_097, "2026-02-24", "k25"),
        fact("Revenues", "2025-01-01", "2025-03-31", 28_247, filed="2026-04-28", accn="q1-26"),
        fact("Revenues", "2025-04-01", "2025-06-30", 27_443, filed="2026-07-28", accn="q2-26"),
    ]
    out = standardize(frame(*rows), DEC)
    q, fy = values(out, "Q"), values(out, "FY")
    assert fy[(2024, 0)][0] == 105_856 and fy[(2025, 0)][0] == 113_097
    assert q[(2025, 1)] == (28_247, "REPORTED") and q[(2025, 2)] == (27_443, "REPORTED")
    assert q[(2025, 3)] == (28_088, "REPORTED")  # its restatement is not filed yet
    assert pd.isna(q[(2025, 4)][0]) and pd.isna(q[(2024, 4)][0])
    # Without a basis change the fourth quarter is derived as before.
    plain = standardize(frame(*rows[:2]), DEC)
    assert values(plain, "Q")[(2024, 4)] == (101_887 - 76_540, "DERIVED")


def test_an_nm_fourth_quarter_is_not_rebuilt_at_concept_level() -> None:
    """compute fills missing quarters from year-to-date values across candidate tags; it must
    not rebuild the fourth quarter left N/M for mixed bases (D-052)."""
    from tests.conftest import CONFIG_DIR
    from uw_pipeline.compute.metrics import compute_metrics
    from uw_pipeline.compute.registry import load_registry
    from uw_pipeline.config import load_industry_config

    rows = [
        fact("Revenues", "2025-01-01", "2025-03-31", 27_366, filed="2025-04-29", accn="q1-25"),
        fact("Revenues", "2025-01-01", "2025-09-30", 81_917, filed="2025-10-30", accn="q3-25"),
        fact("Revenues", "2024-01-01", "2024-12-31", 101_887, form="10-K", filed="2025-02-25",
             accn="k24"),
        fact("Revenues", "2024-01-01", "2024-12-31", 105_856, form="10-K", filed="2026-02-24",
             accn="k25"),
        fact("Revenues", "2025-01-01", "2025-12-31", 113_097, form="10-K", filed="2026-02-24",
             accn="k25"),
    ]  # fmt: skip
    creg = load_registry(CONFIG_DIR).for_company(
        "BAC", load_industry_config(CONFIG_DIR).groups["BANKS"]
    )
    revenue = compute_metrics(standardize(frame(*rows), DEC), creg)["revenue"]
    assert revenue[("FY", 2025, 0)].value == 113_097
    q4 = revenue[("Q", 2025, 4)]
    assert q4.value is None and q4.status == "NM"  # shown as N/M, counted by coverage
