"""D-071: notes payable and convertible notes as their own debt families."""

import pandas as pd

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_industry_config
from uw_pipeline.normalize.periods import learn_calendar, standardize

FY = ("FY", 2026, 0)


def _metrics(tags: dict[str, float]):
    rows = [{
        "taxonomy": "us-gaap", "tag": tag, "dims": "", "unit": "USD", "val": val, "start": None,
        "end": "2026-01-31", "fy": 2026, "fp": "FY", "form": "10-K", "accn": "k",
        "filed": "2026-03-20", "frame": None, "source": "companyfacts",
    } for tag, val in tags.items()]  # fmt: skip
    facts = pd.DataFrame(rows)
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    creg = reg.for_company("RBRK", ind.groups["SOFTWARE_INTERNET"])
    return compute_metrics(standardize(facts, learn_calendar(facts, "0131")), creg)


def test_convertible_notes_alone_are_the_debt() -> None:
    out = _metrics({"ConvertibleLongTermNotesPayable": 1.131e9, "StockholdersEquity": 2e8})
    assert out["total_debt"][FY].value == 1.131e9
    assert out["long_term_debt"][FY].value == 1.131e9


def test_convertible_notes_pair_with_their_current_part() -> None:
    out = _metrics({"ConvertibleLongTermNotesPayable": 1.0e9,
                    "ConvertibleNotesPayableCurrent": 2.5e8})  # fmt: skip
    assert out["total_debt"][FY].value == 1.25e9


def test_the_long_term_debt_family_comes_first() -> None:
    out = _metrics({"LongTermDebtNoncurrent": 5e8, "LongTermDebtCurrent": 1e8,
                    "ConvertibleLongTermNotesPayable": 1e9})  # fmt: skip
    assert out["total_debt"][FY].value == 6e8
    assert out["long_term_debt"][FY].value == 5e8
