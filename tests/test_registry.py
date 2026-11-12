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
        "ppnr", "efficiency_ratio",  # D-096
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


def test_a_view_must_name_another_card() -> None:
    """D-078: a metric shown as a view of another metric's card names a metric that is a card."""
    rev = {"code": "revenue", "display_name": "Revenue", "section": "financial", "unit": "USD",
           "formula": "revenue"}  # fmt: skip
    concepts = {"revenue": {"tags": ["us-gaap:Revenues"]}}
    view = {
        "code": "growth_q",
        "display_name": "Growth (QoQ)",
        "section": "financial",
        "unit": "fraction",
        "kind": "growth",
        "base": "revenue",
        "compare": "prior_quarter",
    }
    for target in ("nonexistent", "growth_q"):
        with pytest.raises(ValidationError, match="view_of"):
            Registry(
                concepts=concepts, metrics=[rev, view | {"view_of": {"of": target, "view": "QoQ"}}]
            )
    reg = Registry(
        concepts=concepts, metrics=[rev, view | {"view_of": {"of": "revenue", "view": "QoQ"}}]
    )
    assert reg.metrics[1].view_of and reg.metrics[1].view_of.of == "revenue"
    # The configured case: Revenue Growth (QoQ) is the QoQ button of the Revenue Growth card.
    real = {m.code: m for m in load_registry(CONFIG_DIR).metrics}
    assert real["revenue_growth_qoq"].view_of.of == "revenue_growth_yoy"
    assert real["revenue_growth_yoy"].card_title == "Revenue Growth"
    assert real["revenue_growth_yoy"].display_name == "Revenue Growth (YoY)"  # LLM inputs as before


def test_company_override_replaces_a_group_metric(reg, industry) -> None:
    """D-090: Amazon reports technology and infrastructure expense, not R&D, so its own ratios
    replace R&D / Revenue; other software companies keep it."""
    amzn = reg.for_company("AMZN", industry.groups["SOFTWARE_INTERNET"])
    codes = {m.code for m in amzn.metrics}
    assert "rnd_to_revenue" not in codes
    assert {"tech_infra_to_revenue", "fulfillment_to_revenue", "amzn_inventory_days"} <= codes
    by_code = {m.code: m for m in amzn.metrics}
    assert by_code["amzn_inventory_days"].display  # inventory_days itself is hidden (D-054)
    assert not by_code.get("inventory_days") or not by_code["inventory_days"].display
    assert all(t.needs_instance for t in amzn.concepts["technology_infrastructure"])
    msft = reg.for_company("MSFT", industry.groups["SOFTWARE_INTERNET"])
    assert "rnd_to_revenue" in {m.code for m in msft.metrics}


def test_override_exclusion_keeps_needed_helpers_hidden(reg, industry) -> None:
    """A metric a company excludes is still computed, not shown, when another needs it."""
    o = reg.overrides["AMZN"].model_copy(update={"exclude_metrics": ["revenue"]})
    r = reg.model_copy(update={"overrides": {**reg.overrides, "AMZN": o}})
    m = {m.code: m for m in r.for_company("AMZN", industry.groups["SOFTWARE_INTERNET"]).metrics}
    assert not m["revenue"].display  # revenue growth and the ratios still need it
    assert m["revenue_growth_yoy"].display


def test_unknown_override_exclusion_rejected() -> None:
    with pytest.raises(ValidationError, match="exclude_metrics lists unknown"):
        Registry.model_validate({
            "concepts": {"revenue": {"tags": ["us-gaap:Revenues"]}},
            "metrics": [{"code": "revenue", "display_name": "Revenue", "subtitle": "x",
                         "section": "financial", "unit": "USD", "formula": "revenue"}],
            "overrides": {"AMZN": {"exclude_metrics": ["rnd_to_revenue"]}},
        })  # fmt: skip


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


def test_every_displayed_metric_has_a_subtitle(reg) -> None:
    # The grey line under the metric's title (metrics.subtitle): an explanation, not the unit.
    tickers = ["", *reg.overrides]
    shown = [m for t in tickers for m in (reg.metrics if not t else reg.overrides[t].metrics)
             if m.display]  # fmt: skip
    missing = sorted(m.code for m in shown if not m.subtitle)
    assert not missing, f"displayed metrics without a subtitle: {missing}"
    units = {"USD", "%", "x", "ratio", "fraction", "days", "count"}
    assert not [m.code for m in shown if m.subtitle in units]
