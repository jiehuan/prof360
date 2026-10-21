import pytest
from pydantic import ValidationError

from tests.conftest import CONFIG_DIR
from uw_pipeline.config import (
    CompaniesFile,
    IndustryConfig,
    load_companies,
    load_industry_config,
)


def test_mvp_company_list() -> None:
    companies = load_companies(CONFIG_DIR)
    assert [c.ticker for c in companies][:9] == [
        "MAR", "LUV", "F", "PFE", "JPM", "M", "LUMN", "SKYD", "ENPH",
    ]  # fmt: skip
    # Phase 16: the S&P 100 (100 companies; GOOGL and GOOG are one) plus seven MVP companies;
    # Phase 17a': 123 more from the development sample (5 per industry group, D-055).
    assert len(companies) == 230 and {"AAPL", "GOOGL", "BRK.B", "XOM", "DHI", "NEM"} <= {
        c.ticker for c in companies
    }
    assert all(c.cik for c in companies), "every company pins its CIK"
    assert len({c.cik for c in companies}) == len(companies), "one entry per CIK"


def test_duplicate_ticker_rejected() -> None:
    with pytest.raises(ValidationError):
        CompaniesFile.model_validate({"companies": [{"ticker": "a"}, {"ticker": "A"}]})


@pytest.mark.parametrize(
    ("sic", "group", "how", "fi"),
    [
        (6021, "BANKS", "sic", True),
        (7011, "HOTELS", "sic", False),
        (6712, "BANKS", "sic", True),
        (6035, "BANKS", "range", True),
        (6211, "BROKER_DEALERS", "range", True),
        (6331, "INSURANCE", "range", False),
        (6798, "REIT", "range", False),  # REITs are not FI (Phase 16 group)
        (None, "UNCLASSIFIED", "default", False),
        # Phase 17 (D-055)
        (5812, "RESTAURANTS", "sic", False),  # exact code inside the retail range
        (6099, "PAYMENTS", "sic", False),  # exact code inside the bank range
        (1311, "ENERGY", "range", False),  # oil and gas inside the mining block
        (1040, "MINING", "range", False),
        (3312, "METALS", "range", False),
        (1531, "HOMEBUILDERS", "range", False),
        (1731, "CONSTRUCTION_ENGINEERING", "range", False),
        (3651, "CONSUMER_DURABLES_APPAREL", "range", False),  # carved out of industrials
        (3743, "INDUSTRIALS", "range", False),  # railroad equipment
        (4610, "ENERGY", "range", False),  # crude pipelines
        (7389, "BUSINESS_SERVICES", "range", False),
        (8731, "PHARMA", "range", False),
        (6770, "UNCLASSIFIED", "default", False),  # SPACs are out of scope
        (4400, "UNCLASSIFIED", "default", False),  # water transportation (PLAN 17, remaining 2%)
    ],
)
def test_classify(sic: int | None, group: str, how: str, fi: bool) -> None:
    g, matched_by = load_industry_config(CONFIG_DIR).classify(sic)
    assert (g.code, matched_by, g.is_financial) == (group, how, fi)


def test_unknown_group_reference_rejected() -> None:
    with pytest.raises(ValidationError, match="NOPE"):
        IndustryConfig.model_validate(
            {
                "default_group": "X",
                "groups": {"X": {"label": "x", "is_financial": False}},
                "sic": {1234: "NOPE"},
            }
        )


def test_overlapping_sic_ranges_rejected() -> None:
    """D-055: a company's group must not depend on the order of the ranges."""
    with pytest.raises(ValidationError, match="overlap: 3000-3299 X and 3100-3199 Y"):
        IndustryConfig.model_validate(
            {
                "default_group": "X",
                "groups": {g: {"label": g, "is_financial": False} for g in ("X", "Y")},
                "sic_ranges": [
                    {"from": 3100, "to": 3199, "group": "Y"},
                    {"from": 3000, "to": 3299, "group": "X"},
                ],
            }
        )


def test_llm_provider_profiles(monkeypatch) -> None:
    """One model profile per provider (D-039): SecureGPT on the corporate network, Claude for
    development off it."""
    import pytest

    from uw_pipeline.enrich.llm import load_llm_config
    from uw_pipeline.enrich.package import load_insights_config

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    default = load_llm_config(CONFIG_DIR)
    assert default.provider == "securegpt"
    assert (default.extractor.model, default.tiebreaker.model, default.synthesis.model) == (
        "gpt-6-luna-2026-09-22", "gpt-6-sol-2026-09-22", "gpt-6-sol-2026-09-22")  # fmt: skip
    assert default.pricing_currency == "EUR"

    claude = load_llm_config(CONFIG_DIR, "anthropic")
    assert (claude.extractor.model, claude.tiebreaker.model, claude.synthesis.model) == (
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5")  # fmt: skip
    assert claude.prompt_version == default.prompt_version  # shared settings stay
    assert load_insights_config(CONFIG_DIR, "anthropic").synthesis.model == "claude-sonnet-5-5"

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert load_llm_config(CONFIG_DIR).provider == "anthropic"
    assert load_llm_config(CONFIG_DIR, "securegpt").provider == "securegpt"  # explicit wins
    with pytest.raises(ValueError, match=r"not in config/llm\.yaml"):
        load_llm_config(CONFIG_DIR, "nope")
