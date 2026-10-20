"""Form 4 parsing, insider metrics, insider cluster signals and Schedule 13D parties (Phase 15b/d).

Form 4 XML and index headers are recorded EDGAR files under tests/fixtures/sec/.
"""

from pathlib import Path

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.insiders import insider_metrics
from uw_pipeline.extract.filings import ownership_parties
from uw_pipeline.jobs.run_documents import _about_company
from uw_pipeline.normalize.form4 import parse_form4
from uw_pipeline.signals.detect import Detected, Doc, insider_signals, ownership_signals
from uw_pipeline.signals.rules import load_signal_rules

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
RULES = load_signal_rules(CONFIG_DIR)


def _form4(name: str) -> dict:
    return parse_form4((FIXTURES / "form4" / name).read_bytes())


def test_parse_form4_plan_footnote_applies_per_line() -> None:
    # Enphase CFO: the 10b5-1 box is ticked, but only the sale cites the 10b5-1 footnote; the
    # tax-withholding line (F) cites another footnote.
    parsed = _form4("enph_plan_sale.xml")
    assert parsed["issuer_cik"] == 1463101
    assert parsed["owner"] == {
        "cik": "0001645551", "name": "Yang Mandy", "is_director": False, "is_officer": True,
        "officer_title": "EVP, Chief Financial Officer", "is_ten_percent": False,
    }  # fmt: skip
    assert [(t["code"], t["trade_date"], t["shares"], t["price"], t["shares_after"],
             t["is_10b5_1"]) for t in parsed["transactions"]] == [
        ("F", "2025-12-10", 1319, 32.79, 74987, False),
        ("S", "2025-12-12", 6000, 33.01, 68987, True),
    ]  # fmt: skip


def test_parse_form4_director_sale_outside_plan() -> None:
    parsed = _form4("enph_director_sale.xml")
    assert parsed["owner"]["name"] == "MORA RICHARD"
    assert parsed["owner"]["is_director"] and not parsed["owner"]["is_officer"]
    [t] = parsed["transactions"]
    assert (t["code"], t["shares"], t["price"], t["direct"], t["is_10b5_1"]) == (
        "S", 1100, 52.0488, True, False,
    )  # fmt: skip


def test_parse_form4_filed_by_the_company_about_another_issuer() -> None:
    # JPMorgan's EDGAR list also holds Form 4s it files as a 10% owner of other issuers.
    parsed = _form4("jpm_as_owner.xml")
    assert parsed["issuer_cik"] != 19617
    assert parsed["owner"]["is_ten_percent"] and not parsed["owner"]["is_officer"]


_BOX_ONLY = b"""<?xml version="1.0"?><ownershipDocument>
<issuer><issuerCik>0000000042</issuerCik></issuer><aff10b5One>%s</aff10b5One>
<nonDerivativeTable><nonDerivativeTransaction><transactionCoding><transactionCode>S
</transactionCode></transactionCoding>%s</nonDerivativeTransaction></nonDerivativeTable>
<footnotes><footnote id="F1">%s</footnote></footnotes></ownershipDocument>"""


def test_plan_box_applies_when_no_line_cites_a_plan_footnote() -> None:
    def flag(box: bytes, cite: bytes, note: bytes) -> bool:
        return parse_form4(_BOX_ONLY % (box, cite, note))["transactions"][0]["is_10b5_1"]

    assert flag(b"1", b"", b"Weighted average price.")
    assert not flag(b"0", b"", b"Weighted average price.")
    cite = b'<footnoteId id="F1"/>'
    assert flag(b"0", cite, b"Sold under a Rule 10b5-1 trading plan adopted May 1, 2026.")
    assert not flag(b"0", cite, b"These shares were not sold pursuant to a Rule 10b5-1 plan.")


def _trade(day: str, owner: str, code: str = "S", shares: float = 100, price: float = 10,
           after: float = 900, *, officer: bool = True, director: bool = False,
           plan: bool = False, line: int = 1) -> dict:  # fmt: skip
    return {
        "accession_no": f"{owner}-{day}",
        "line": line,
        "owner_cik": owner,
        "owner_name": owner,
        "is_officer": officer,
        "is_director": director,
        "trade_date": day,
        "code": code,
        "shares": shares,
        "price": price,
        "shares_after": after,
        "is_10b5_1": plan,
    }


def test_insider_metrics_by_fiscal_quarter() -> None:
    periods = {("Q", 2026, 1): "2026-03-31", ("Q", 2026, 2): "2026-06-30"}
    rows = [
        _trade("2026-02-01", "A", after=900),
        _trade("2026-03-01", "A", after=800),
        _trade("2026-03-05", "B", "P", shares=50, officer=False, director=True),
        _trade("2026-03-06", "C", officer=False),  # a 10% owner: not an insider here
        _trade("2026-03-07", "A", "M", shares=500),  # option exercise: not open market
    ]
    out = insider_metrics(rows, periods)
    assert out["insider_net_sales"] == {("Q", 2026, 1): (2000 - 500, "2026-03-31")}
    # A sold 200 of the 1,000 shares held before the quarter's sales.
    assert out["officer_sell_pct"] == {("Q", 2026, 1): (0.2, "2026-03-31")}
    # A quarter wholly after the start of the Form 4s read, without trades, is 0 (none sold).
    out = insider_metrics(rows, periods, covered_from="2025-12-31")
    assert out["insider_net_sales"][("Q", 2026, 2)] == (0.0, "2026-06-30")
    assert out["officer_sell_pct"][("Q", 2026, 2)] == (0.0, "2026-06-30")
    # Q2 began before the Form 4s read (April 15): no value without trades.
    partly = insider_metrics(rows, periods, covered_from="2026-04-15")
    assert partly["insider_net_sales"].keys() == {("Q", 2026, 1)}


def _drop(day: str) -> Detected:
    return Detected("stock_drop", "Event Signals", "Stock Drop", "Warning",
                    "Single-day stock drop", "", day)  # fmt: skip


CLUSTER = [
    _trade("2026-03-02", "A"),
    _trade("2026-03-03", "X", plan=True),  # under a 10b5-1 plan: left out
    _trade("2026-03-04", "Y", officer=False),  # not an officer or director
    _trade("2026-03-05", "B", officer=False, director=True),
    _trade("2026-03-10", "C"),
    _trade("2026-03-20", "D"),
    _trade("2026-04-10", "E"),  # after the episode; only 2 sellers in its own window
    _trade("2023-01-05", "F"),
    _trade("2023-01-06", "G"),
    _trade("2023-01-07", "H"),  # too old
]


def test_insider_cluster_is_one_signal_per_episode() -> None:
    [s] = insider_signals(CLUSTER, [], RULES, "2026-10-13")
    assert (s.rule_id, s.severity, s.event_date, s.key_extra) == (
        "insider_cluster_selling", "Info", "2026-03-20", "2026-03-02",
    )  # fmt: skip
    assert s.description.startswith(
        "4 officers or directors sold 0.0M USD of shares on the open market between "
        "2026-03-02 and 2026-03-20, outside Rule 10b5-1 plans: A, B, C, D."
    )


def test_insider_cluster_followed_by_a_drop_is_a_warning() -> None:
    [s] = insider_signals(CLUSTER, [_drop("2026-05-01")], RULES, "2026-10-13")
    assert s.severity == "Warning"
    assert s.description.endswith("Followed by: single-day stock drop on 2026-05-01.")
    [s] = insider_signals(CLUSTER, [_drop("2026-12-01")], RULES, "2026-12-31")
    assert s.severity == "Info"  # more than escalate_days after the last sale


def _header(cik: str, accn: str) -> str:
    path = FIXTURES / "filings" / f"CIK{cik}" / accn / f"{accn}-index-headers.html"
    return path.read_text()


def test_ownership_parties_from_index_headers() -> None:
    assert ownership_parties(_header("0000092380", "0000902664-24-005965")) == (
        92380, "Elliott Investment Management L.P.",
    )  # fmt: skip
    # JPMorgan's own 13D about a fund: listed under JPM's CIK, but not about JPM.
    assert ownership_parties(_header("0000019617", "0001193125-26-086018")) == (
        890196, "JPMORGAN CHASE & CO",
    )  # fmt: skip
    assert ownership_parties("<html>no header</html>") == (None, None)


def test_only_13ds_about_the_company_are_kept(sec_routes, make_sec_client) -> None:
    sec = make_sec_client()
    assert _about_company(sec, "0000092380", "0000902664-24-005965")
    assert not _about_company(sec, "0000019617", "0001193125-26-086018")


def test_13d_signal_names_the_filer() -> None:
    docs = [Doc(1, "SC 13D/A", "PRIMARY", "0000902664-24-005965", "2024-10-24", None, None)]
    [s] = ownership_signals(docs, RULES, {"0000902664-24-005965": "Elliott Investment"})
    assert s.description.startswith("Elliott Investment filed a SC 13D/A about the company")
