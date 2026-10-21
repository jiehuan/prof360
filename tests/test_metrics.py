import pandas as pd
import pytest

from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import CompanyRegistry, MetricDef, TagRef
from uw_pipeline.normalize.periods import PERIOD_COLUMNS


def p(tag, ptype, fy, fq, value, *, derivation="REPORTED", accn="a", filed="2026-01-01"):
    tax, name = tag.split(":")
    end = f"{fy}-12-31" if ptype == "FY" else f"{fy}-{3 * fq:02d}-28"
    return {
        "taxonomy": tax, "tag": name, "dims": "", "unit": "USD", "period_type": ptype,
        "fiscal_year": fy, "fiscal_quarter": fq, "period_end": end, "value": float(value),
        "derivation": derivation, "accn": accn, "filed": filed,
    }  # fmt: skip


def registry(concepts: dict[str, list[str]], metrics: list[dict]) -> CompanyRegistry:
    return CompanyRegistry(
        concepts={c: [TagRef(tag=t) for t in tags] for c, tags in concepts.items()},
        metrics=[
            MetricDef.model_validate({"display_name": m["code"], "section": "x", "unit": "USD"} | m)
            for m in metrics
        ],
    )


def run(rows, reg):
    return compute_metrics(pd.DataFrame(rows, columns=PERIOD_COLUMNS), reg)


def test_tag_is_chosen_per_period() -> None:
    reg = registry({"ie": ["us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating"]},
                   [{"code": "ie", "formula": "ie"}])  # fmt: skip
    out = run(
        [p("us-gaap:InterestExpense", "FY", 2023, 0, 10),
         p("us-gaap:InterestExpenseNonoperating", "FY", 2024, 0, 11),
         p("us-gaap:InterestExpenseNonoperating", "FY", 2023, 0, 99)],  # lower priority
        reg,
    )  # fmt: skip
    got = {k: (v.value, v.formula) for k, v in out["ie"].items()}
    assert got == {
        ("FY", 2023, 0): (10, "us-gaap:InterestExpense"),
        ("FY", 2024, 0): (11, "us-gaap:InterestExpenseNonoperating"),
    }


def test_formula_alternatives_optional_and_nm() -> None:
    reg = registry(
        {
            "total": ["us-gaap:DebtAndCapitalLeaseObligations"],
            "nc": ["us-gaap:LongTermDebtNoncurrent"],
            "cur": ["us-gaap:LongTermDebtCurrent"],
            "stb": ["us-gaap:ShortTermBorrowings"],
            "equity": ["us-gaap:StockholdersEquity"],
        },
        [
            {"code": "debt", "formula": ["total", "nc + cur + stb"], "optional": ["stb"]},
            {"code": "de", "formula": "debt / equity", "nm_when": "equity <= 0", "unit": "ratio"},
        ],
    )
    out = run(
        [
            p("us-gaap:DebtAndCapitalLeaseObligations", "FY", 2025, 0, 100),
            p("us-gaap:LongTermDebtNoncurrent", "FY", 2024, 0, 70),
            p("us-gaap:LongTermDebtCurrent", "FY", 2024, 0, 20),
            p("us-gaap:LongTermDebtNoncurrent", "FY", 2023, 0, 70),  # no current: pair incomplete
            p("us-gaap:StockholdersEquity", "FY", 2025, 0, -5),
            p("us-gaap:StockholdersEquity", "FY", 2024, 0, 45),
        ],
        reg,
    )
    debt = {k[1]: (v.value, v.derivation) for k, v in out["debt"].items()}
    assert debt == {2025: (100, "REPORTED"), 2024: (90, "COMPUTED")}
    de = {k[1]: (v.value, v.status) for k, v in out["de"].items()}
    assert de == {2025: (None, "NM"), 2024: (2.0, "OK")}


def test_nm_when_counts_a_missing_optional_input_as_zero() -> None:
    # near_term_debt_coverage: a company without short-term investments is still checked.
    reg = registry(
        {"due": ["us-gaap:LongTermDebtCurrent"], "cash": ["us-gaap:Cash"],
         "sti": ["us-gaap:ShortTermInvestments"], "ocf": ["us-gaap:OCF"]},
        [{"code": "cov", "formula": "due / (cash + sti + ocf)", "optional": ["sti"],
          "nm_when": "cash + sti + ocf <= 0", "unit": "ratio"}],
    )  # fmt: skip
    out = run(
        [p("us-gaap:LongTermDebtCurrent", "FY", y, 0, 10) for y in (2024, 2025)]
        + [p("us-gaap:Cash", "FY", 2024, 0, 5), p("us-gaap:OCF", "FY", 2024, 0, -9),
           p("us-gaap:Cash", "FY", 2025, 0, 5), p("us-gaap:OCF", "FY", 2025, 0, 10),
           p("us-gaap:ShortTermInvestments", "FY", 2025, 0, 5)],
        reg,
    )  # fmt: skip
    cov = {k[1]: (v.value, v.status) for k, v in out["cov"].items()}
    assert cov == {2024: (None, "NM"), 2025: (0.5, "OK")}


def test_division_by_zero_gives_no_value() -> None:
    reg = registry({"a": ["us-gaap:A"], "b": ["us-gaap:B"]},
                   [{"code": "r", "formula": "a / b", "unit": "ratio"}])  # fmt: skip
    out = run([p("us-gaap:A", "FY", 2025, 0, 1), p("us-gaap:B", "FY", 2025, 0, 0)], reg)
    assert out["r"] == {}


def test_growth_qoq_and_yoy() -> None:
    reg = registry(
        {"rev": ["us-gaap:Revenues"]},
        [
            {"code": "revenue", "formula": "rev"},
            {"code": "qoq", "kind": "growth", "base": "revenue", "compare": "prior_quarter",
             "unit": "fraction", "period_types": ["Q"]},
            {"code": "yoy", "kind": "growth", "base": "revenue", "compare": "prior_year",
             "unit": "fraction"},
        ],
    )  # fmt: skip
    out = run(
        [p("us-gaap:Revenues", "Q", 2024, 4, 100), p("us-gaap:Revenues", "Q", 2025, 1, 110),
         p("us-gaap:Revenues", "Q", 2024, 1, 50), p("us-gaap:Revenues", "Q", 2023, 4, 0)],
        reg,
    )  # fmt: skip
    assert out["qoq"][("Q", 2025, 1)].value == pytest.approx(0.10)  # crosses fiscal year
    assert out["yoy"][("Q", 2025, 1)].value == pytest.approx(1.20)
    assert ("Q", 2024, 4) not in out["qoq"]  # no Q3 2024
    assert ("Q", 2024, 1) not in out["qoq"]  # comparison value is 0 -> no growth


def test_quarter_derived_across_tag_switch() -> None:
    """MAR: 10-Qs tag capex as PP&E payments, the 10-K as productive-asset payments."""
    reg = registry(
        {"capex": ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                   "us-gaap:PaymentsToAcquireProductiveAssets"]},
        [{"code": "capex", "formula": "capex"}],
    )  # fmt: skip
    ppe, prod = (
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:PaymentsToAcquireProductiveAssets",
    )
    out = run(
        [p(ppe, "Q", 2025, 1, 10), p(ppe, "Q", 2025, 2, 15), p(ppe, "Q", 2025, 3, 20),
         p(ppe, "YTD", 2025, 3, 45), p(prod, "FY", 2025, 0, 70)],
        reg,
    )  # fmt: skip
    q4 = out["capex"][("Q", 2025, 4)]
    assert (q4.value, q4.derivation) == (25, "DERIVED")
    assert q4.source_map() == {"capex": [prod, ppe]}
    assert not any(k[0] == "YTD" for k in out["capex"])


def test_trailing_four_quarters_and_cash_runway() -> None:
    """D-058: `x_ttm` sums the four quarters ending at the period (a year uses its own value);
    cash runway in months is N/M without a cash burn."""
    reg = registry(
        {"cash": ["us-gaap:CashAndCashEquivalentsAtCarryingValue"],
         "sti": ["us-gaap:ShortTermInvestments"],
         "ocf": ["us-gaap:NetCashProvidedByUsedInOperatingActivities"]},
        [{"code": "burn", "formula": ["-ocf_ttm / 12", "-ocf / 3"]},
         {"code": "runway", "unit": "months", "formula": "(cash + sti) / burn",
          "optional": ["sti"], "nm_when": "burn <= 0"}],
    )  # fmt: skip
    ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
    rows = [p(ocf, "Q", 2025, q, -30) for q in (2, 3, 4)] + [
        p(ocf, "Q", 2026, 1, -30), p(ocf, "Q", 2026, 2, 10), p(ocf, "FY", 2025, 0, -100),
        p("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Q", 2026, 1, 300),
        p("us-gaap:ShortTermInvestments", "Q", 2026, 1, 60),
        p("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Q", 2026, 2, 280),
        p("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Q", 2025, 2, 500),
        p("us-gaap:CashAndCashEquivalentsAtCarryingValue", "FY", 2025, 0, 400),
    ]  # fmt: skip
    out = {k: (v.value, v.status) for k, v in run(rows, reg)["runway"].items()}
    assert out[("Q", 2026, 1)] == (pytest.approx(360 / (120 / 12)), "OK")  # Q2-Q4 2025, Q1 2026
    assert out[("Q", 2026, 2)] == (pytest.approx(280 / (80 / 12)), "OK")  # -30*3 + 10 = -80
    assert out[("FY", 2025, 0)] == (pytest.approx(400 / (100 / 12)), "OK")  # the year itself
    # Three quarters before Q2 2025 are missing: that quarter's own burn (30 a quarter).
    assert out[("Q", 2025, 2)] == (pytest.approx(500 / (30 / 3)), "OK")


def test_trailing_sum_without_burn_is_not_meaningful() -> None:
    reg = registry(
        {"cash": ["us-gaap:Cash"], "ocf": ["us-gaap:NetCashProvidedByUsedInOperatingActivities"]},
        [{"code": "burn", "formula": ["-ocf_ttm / 12", "-ocf / 3"]},
         {"code": "runway", "unit": "months", "formula": "cash / burn", "nm_when": "burn <= 0"}],
    )  # fmt: skip
    rows = [p("us-gaap:NetCashProvidedByUsedInOperatingActivities", "Q", 2026, q, 5)
            for q in (1, 2, 3, 4)] + [p("us-gaap:Cash", "Q", 2026, 4, 100)]  # fmt: skip
    [(value, status)] = [(v.value, v.status) for v in run(rows, reg)["runway"].values()]
    assert (value, status) == (None, "NM")
