"""Phase 14a (decisions D-043): bank profitability and credit metrics, prior-year averages,
traits, and the daily LLM budget. Golden values: JPMorgan's FY2025 10-K (0001628280-26-008131)."""

from datetime import UTC, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from tests.conftest import CONFIG_DIR
from tests.fake_llm import FakeLLM
from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_industry_config
from uw_pipeline.enrich.llm import DailyBudget, ModelSpec, cached_call
from uw_pipeline.normalize.periods import learn_calendar, standardize
from uw_pipeline.schema import llm_call, metadata

ACCN = "0001628280-26-008131"
# us-gaap tag -> (FY2024, FY2025), in USD, as reported in the FY2025 10-K.
JPM = {
    "Assets": (4_002_814e6, 4_424_900e6),
    "StockholdersEquity": (344_758e6, 362_438e6),
    "NetIncomeLoss": (58_471e6, 57_048e6),
    "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest": (24_345e6, 25_765e6),
    "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss": (
        1_299_590e6,
        1_408_905e6,
    ),
    "InterestIncomeExpenseNet": (92_583e6, 95_443e6),
    "NoninterestIncome": (84_973e6, 87_004e6),
}
INSTANTS = {"Assets", "StockholdersEquity",
            "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest",
            "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss"}  # fmt: skip


def _facts() -> pd.DataFrame:
    rows = []
    for tag, values in JPM.items():
        for year, val in zip((2024, 2025), values, strict=True):
            rows.append({
                "taxonomy": "us-gaap", "tag": tag, "dims": "", "unit": "USD", "val": val,
                "start": None if tag in INSTANTS else f"{year}-01-01", "end": f"{year}-12-31",
                "fy": 2025, "fp": "FY", "form": "10-K", "accn": ACCN, "filed": "2026-02-13",
                "frame": None, "source": "companyfacts",
            })  # fmt: skip
    return pd.DataFrame(rows)


def test_bank_metrics_from_the_10k() -> None:
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    creg = reg.for_company("JPM", ind.groups["BANKS"])
    facts = _facts()
    out = compute_metrics(standardize(facts, learn_calendar(facts, "1231")), creg)
    fy = ("FY", 2025, 0)
    # Averages use the prior year-end (`total_assets_py`): 57,048 / ((4,424,900 + 4,002,814)/2)
    assert out["roa"][fy].value == pytest.approx(57_048 / ((4_424_900 + 4_002_814) / 2))
    assert out["roe"][fy].value == pytest.approx(57_048 / ((362_438 + 344_758) / 2))
    assert out["loan_loss_reserve_ratio"][fy].value == pytest.approx(25_765 / 1_408_905)
    assert out["fee_to_net_interest_income"][fy].value == pytest.approx(87_004 / 95_443)
    # FY2024 has no prior year in the input: no average, no value.
    assert ("FY", 2024, 0) not in out["roa"]


def test_traits_add_metric_sets() -> None:
    ind = load_industry_config(CONFIG_DIR)
    assert {"HOTEL_OPERATING", "GOVERNANCE"} <= set(ind.groups["HOTELS"].metric_sets)
    assert {"BANK_CORE", "BANK_REGULATORY"} <= set(ind.groups["BANKS"].metric_sets)
    assert "CONTRACT_REVENUE" in ind.groups["SEMICONDUCTORS"].metric_sets
    assert "CONTRACT_REVENUE" not in ind.groups["HOTELS"].metric_sets


def test_daily_budget_stops_new_calls_but_not_the_cache() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = datetime(2026, 10, 14, 15, tzinfo=UTC)
    spec = ModelSpec(model="m", effort="low", max_tokens=100)
    with engine.begin() as conn:  # committed first: on SQL Server the cache row is written in
        # its own transaction, which would wait for an uncommitted insert here.
        # $24 already spent today: 12M input tokens at $2 per million.
        conn.execute(insert(llm_call).values(
            input_hash="x", task="t", prompt_version="v", model="m", output_json="{}",
            input_tokens=12_000_000, output_tokens=0, created_at=now))  # fmt: skip
    with engine.begin() as conn:
        llm = FakeLLM(lambda model, user: {"n": 1})
        llm.budget = DailyBudget(25.0, {"m": (2.0, 10.0)}, "USD")

        def call(user: str):
            return cached_call(conn, llm, spec, task="t", prompt_version="v", system="s",
                               user=user, schema={}, now=now)  # fmt: skip

        assert call("a") == {"n": 1}  # $24 < $25: allowed
        llm.budget.spent = 25.0
        assert call("b") is None  # over budget: this item waits for tomorrow
        assert call("a") == {"n": 1}  # cached: free, still served
        assert len(llm.calls) == 1


def test_nim_relation_annualizes_a_quarter() -> None:
    """JPM Q2 2026: NII 25,511 (reported) x 4 / average interest-earning assets 4,287,954 =
    2.38%, against the 2.40% net yield it reports (managed basis): within 5%."""
    from uw_pipeline.quality.dq import load_dq_config

    nim = next(i for i in load_dq_config(CONFIG_DIR).metric_identities.checks
               if i.name == "nim_relation")  # fmt: skip
    q = {"nim": 0.0240, "net_interest_income": 25_511e6, "avg_interest_earning_assets": 4_287_954e6}
    assert nim.check(q, "Q") is None
    assert nim.check(q, "FY") is not None  # not annualized: 0.6%, far from 2.40%
    assert nim.check(q | {"nim": 0.0340}, "Q") is not None


def test_debt_without_current_portion_is_reported() -> None:
    """Macy's: no current-portion tag, so total debt is the non-current fallback (D-046)."""
    from uw_pipeline.quality.dq import _Rule, debt_current_portion

    row = {"metric_code": "total_debt", "period_type": "FY", "fiscal_year": 2025,
           "fiscal_quarter": 0, "value": 2_430e6}  # fmt: skip
    fallback = row | {"formula": "ltdcl_noncurrent + short_term_borrowings"}
    paired = row | {"formula": "ltdcl_noncurrent + ltdcl_current + short_term_borrowings"}
    assert [f.rule_id for f in debt_current_portion([fallback], _Rule(severity="Warning"))] == [
        "debt_current_portion"]  # fmt: skip
    assert debt_current_portion([paired], _Rule(severity="Warning")) == []


def test_group_notes_apply_to_every_company_of_the_group(tmp_path) -> None:
    # Phase 16 (16c): `groups:` notes cover a gap a whole industry shares; a company note wins.
    from uw_pipeline.compute.coverage import load_notes

    (tmp_path / "coverage_notes.yaml").write_text(
        "groups:\n"
        "  '*':\n"
        "    goodwill_impairment: Tagged only in impairment years.\n"
        "  INSURANCE:\n"
        "    current_ratio: Insurers do not classify the balance sheet as current.\n"
        "    gross_margin: Not reported.\n"
        "BRK.B:\n"
        "  gross_margin: Berkshire reports segment margins only.\n"
    )
    notes = load_notes(tmp_path)
    brk = notes.for_company("brk.b", "INSURANCE")
    assert brk["current_ratio"].startswith("Insurers")
    assert brk["gross_margin"].startswith("Berkshire")
    assert notes.for_company("AAPL", "HARDWARE") == {
        "goodwill_impairment": "Tagged only in impairment years."}  # fmt: skip


def test_a_ratio_over_a_text_metric_gets_the_text_target() -> None:
    """Phase 16: top_product_share = top_product_revenue (text, last ~400 days) / revenue can
    only reach as far back as its text input: 4 quarters, no fiscal years."""
    from tests.conftest import CONFIG_DIR
    from uw_pipeline.compute.coverage import company_coverage
    from uw_pipeline.compute.registry import load_registry
    from uw_pipeline.config import load_industry_config

    creg = load_registry(CONFIG_DIR).for_company(
        "PFE", load_industry_config(CONFIG_DIR).groups["PHARMA"]
    )
    rows = [
        {"metric_code": "top_product_share", "period_type": "Q", "fiscal_year": 2026,
         "fiscal_quarter": q, "period_end": f"2026-0{q * 3}-30", "value": 0.1,
         "value_status": "OK", "derivation": "COMPUTED", "source_tags": "{}"}
        for q in (1, 2)
    ] + [
        {"metric_code": "revenue", "period_type": "Q", "fiscal_year": fy, "fiscal_quarter": q,
         "period_end": "x", "value": 1.0, "value_status": "OK", "derivation": "REPORTED",
         "source_tags": "{}"}
        for fy in (2025, 2026) for q in (1, 2, 3, 4) if (fy, q) <= (2026, 2)
    ]  # fmt: skip
    cov = company_coverage("PFE", creg.metrics, rows, {})
    share = next((m, c) for m, c in cov.metrics if m.code == "top_product_share")
    assert share[1].target(share[0]) == (0, 4)
    revenue = next((m, c) for m, c in cov.metrics if m.code == "revenue")
    assert revenue[1].target(revenue[0]) == (5, 8)
