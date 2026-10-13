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
        "buybacks", "buyback_intensity",
        "cet1_ratio", "tier1_ratio", "lcr", "nim", "npl_ratio",  # text-extracted (BANK_REGULATORY)
    }  # fmt: skip
    assert jpm.concepts["revenue"] == [TagRef(tag="us-gaap:RevenuesNetOfInterestExpense")]


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
