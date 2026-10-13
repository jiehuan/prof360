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
    assert [c.ticker for c in companies] == [
        "MAR",
        "LUV",
        "F",
        "PFE",
        "JPM",
        "M",
        "LUMN",
        "SKYD",
        "ENPH",
    ]
    assert all(c.cik for c in companies), "every MVP company pins its CIK"


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
        (6798, "UNCLASSIFIED", "default", False),  # REITs are not FI
        (None, "UNCLASSIFIED", "default", False),
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
