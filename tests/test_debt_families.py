"""D-071: notes payable and convertible notes as their own debt families; D-073: order and
company tags where one figure sat outside the others."""

import pandas as pd

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.metrics import compute_metrics
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import load_industry_config
from uw_pipeline.normalize.periods import learn_calendar, standardize

FY = ("FY", 2026, 0)


def _metrics(tags: dict[str, float], ticker="RBRK", group="SOFTWARE_INTERNET", end="2026-01-31"):
    rows = [{
        "taxonomy": "us-gaap", "tag": tag, "dims": "", "unit": "USD", "val": val, "start": None,
        "end": end, "fy": int(end[:4]), "fp": "FY", "form": "10-K", "accn": "k",
        "filed": "2026-03-20", "frame": None, "source": "companyfacts",
    } for tag, val in tags.items()]  # fmt: skip
    facts = pd.DataFrame(rows)
    reg, ind = load_registry(CONFIG_DIR), load_industry_config(CONFIG_DIR)
    creg = reg.for_company(ticker, ind.groups[group])
    return compute_metrics(standardize(facts, learn_calendar(facts, end[5:7] + end[8:])), creg)


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


FY25 = ("FY", 2025, 0)


def test_a_small_note_does_not_replace_long_term_debt() -> None:
    """Labcorp tags $1-5M of notes payable beside $5B of long-term debt whose current part is
    untagged in some quarters: the long-term debt family wins (D-073)."""
    tags = {"LongTermDebtNoncurrent": 5.569e9, "LongTermNotesPayable": 4e5,
            "NotesPayableCurrent": 2e5}  # fmt: skip
    out = _metrics(tags, "LH", "HEALTHCARE_SERVICES", "2025-12-31")
    assert out["total_debt"][FY25].value == 5.569e9


def test_debt_and_finance_leases_with_all_current_debt() -> None:
    """Valero: the combined debt amount leaves out finance leases; total debt is the balance
    sheet's non-current debt and finance lease obligations plus the current portion (D-073)."""
    tags = {"LongTermDebtAndCapitalLeaseObligations": 9.67e9, "DebtCurrent": 9.49e8,
            "DebtLongtermAndShorttermCombinedAmount": 8.261e9}  # fmt: skip
    out = _metrics(tags, "VLO", "ENERGY", "2025-12-31")
    assert out["total_debt"][FY25].value == 10.619e9
    assert out["long_term_debt"][FY25].value == 9.67e9


def test_homebuilder_notes_not_a_small_long_term_debt_item() -> None:
    """PulteGroup's us-gaap:LongTermDebt is a $44M item beside $1.6B of notes (D-073)."""
    out = _metrics({"NotesPayable": 1.631e9, "LongTermDebt": 4.39e7}, "PHM", "HOMEBUILDERS",
                   "2025-12-31")  # fmt: skip
    assert out["total_debt"][FY25].value == 1.631e9
    assert out["long_term_debt"][FY25].value == 1.631e9


def test_unclassified_debt_is_the_balance_sheet_line() -> None:
    """Park Hotels: debt net of issuance costs, not the gross amount of the debt note (D-073)."""
    out = _metrics({"DebtAndCapitalLeaseObligations": 3.838e9, "LongTermDebt": 3.856e9},
                   "PK", "HOTELS", "2025-12-31")  # fmt: skip
    assert out["total_debt"][FY25].value == 3.838e9
    assert out["long_term_debt"][FY25].value == 3.838e9
