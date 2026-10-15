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
