import pytest
from pydantic import ValidationError

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.registry import Registry, TagRef, load_registry
from uw_pipeline.config import load_industry_config


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(CONFIG_DIR)


@pytest.fixture(scope="module")
def industry():
    return load_industry_config(CONFIG_DIR)


def test_every_group_references_known_metrics(reg, industry) -> None:
    reg.check_groups(industry.groups.values())


def test_metric_list_comes_from_group_config(reg, industry) -> None:
    jpm = reg.for_company("JPM", industry.groups["BANKS"])
    assert {m.code for m in jpm.metrics} == {
        "revenue", "revenue_growth_qoq", "revenue_growth_yoy", "net_income", "equity",
        "buybacks", "buyback_intensity", "noninterest_income", "fee_income_growth_yoy",
        "roa", "roe", "loan_loss_reserve_ratio", "net_interest_income",
        "fee_to_net_interest_income",  # D-043
        "cet1_ratio", "tier1_ratio", "total_capital_ratio", "lcr", "nim", "npl_ratio",
        "total_loans", "avg_interest_earning_assets", "aum", "var_total_avg",
        "international_revenue",  # BANK_REGULATORY
        "ceo_total_comp", "ceo_comp_actually_paid", "neo_avg_total_comp", "tsr_value_100",
        "peer_tsr_value_100",  # GOVERNANCE (every group)
        "goodwill_to_equity", "goodwill_impairment", "sbc_to_revenue", "diluted_shares",
        "diluted_shares_growth_yoy",  # CAPITAL_QUALITY (every group)
        "market_cap", "volatility_90d", "max_drawdown_52w", "eps_surprise_pct",
        "insider_net_sales", "officer_sell_pct",  # MARKET (every group, external)
        "deposits", "deposit_growth_yoy", "uninsured_deposits", "uninsured_deposit_share",
        "cre_loans", "cre_loan_share", "securities_unrealized_loss",
        "unrealized_loss_to_equity",  # BANK_DEPOSITS (Phase 16)
        "bank_net_revenue",  # hidden helper of revenue's second form (Phase 16)
    }  # fmt: skip
    # Bank revenue: net of interest expense; us-gaap:Revenues where only that is tagged (BAC).
    assert jpm.concepts["revenue"] == [
        TagRef(tag="us-gaap:RevenuesNetOfInterestExpense"),
        TagRef(tag="us-gaap:Revenues"),
    ]
    # Capital ratios: JPM's override tags, firm-wide Standardized, unit pure (D-042).
    assert jpm.units["cet1_ratio"] == "pure" and jpm.concepts["cet1_ratio"][0].needs_instance


def test_regulatory_thresholds_cannot_be_ratio_candidates() -> None:
    from uw_pipeline.compute.registry import ConceptDef

    with pytest.raises(ValidationError, match="threshold"):
        ConceptDef.model_validate({"unit": "pure", "tags": [
            "us-gaap:BankingRegulationCommonEquityTierOneRiskBasedCapitalRatioCapitalAdequacyMinimum"
        ]})  # fmt: skip


def test_helper_dependencies_and_company_override(reg, industry) -> None:
    mar = reg.for_company("MAR", industry.groups["HOTELS"])
    codes = [m.code for m in mar.metrics]
    assert "ebit" in codes  # pulled in by interest_coverage
    assert codes.index("ebit") < codes.index("interest_coverage")
    assert {"gross_fee_revenue", "net_fee_revenue"} <= set(codes)
    fee = mar.concepts["gross_fee_revenue"][0]
    assert fee.needs_instance and fee.dims_key == "srt:ProductOrServiceAxis=mar:FeeServiceMember"
    assert any(t.tag == "mar:DepreciationAmortizationAndOther" for t in mar.concepts["d_and_a"])
    # Overrides are per company.
    luv = reg.for_company("LUV", industry.groups["AIRLINES"])
    assert "gross_fee_revenue" not in {m.code for m in luv.metrics}


def test_unknown_metric_input_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        Registry.model_validate(
            {
                "concepts": {"a": {"tags": ["us-gaap:A"]}},
                "metrics": [
                    {
                        "code": "m",
                        "display_name": "m",
                        "section": "s",
                        "unit": "USD",
                        "formula": "a + b",
                    }
                ],
            }
        )


def test_unknown_metric_in_set_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown metrics"):
        Registry.model_validate({"concepts": {}, "metrics": [], "metric_sets": {"S": ["nope"]}})


def test_instance_concepts_only_for_companies_whose_metrics_use_them() -> None:
    """Phase 17: casino revenue is a dimensional fact read from each filing's XBRL instance;
    a software company must not read instances for it."""
    reg = load_registry(CONFIG_DIR)
    ind = load_industry_config(CONFIG_DIR)

    def instance_concepts(ticker: str, group: str) -> set[str]:
        creg = reg.for_company(ticker, ind.groups[group])
        return {code for code, tags in creg.concepts.items()
                if any(t.needs_instance and t.taxonomy != "ecd" for t in tags)}  # fmt: skip

    assert instance_concepts("MSFT", "SOFTWARE_INTERNET") == set()
    assert "casino_revenue" in instance_concepts("MGM", "LEISURE_GAMING")
    assert instance_concepts("MCD", "RESTAURANTS") == {"franchise_revenue"}  # its own tag
    # Concepts read from companyfacts stay for every company (other stages read their facts).
    assert "labor_expense" in reg.for_company("MSFT", ind.groups["SOFTWARE_INTERNET"]).concepts
