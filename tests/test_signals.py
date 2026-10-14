import pytest

from tests.conftest import CONFIG_DIR
from uw_pipeline.signals.detect import (
    Doc,
    auditor_info,
    auditor_signals,
    eight_k_signals,
    keyword_signals,
    metric_signals,
    sentences,
)
from uw_pipeline.signals.rules import load_signal_rules

RULES = load_signal_rules(CONFIG_DIR)


def eight_k(doc_id, items, filed="2026-01-02"):
    return Doc(doc_id, "8-K", "PRIMARY", f"acc-{doc_id}", filed, None, items)


def run_502(text, own=("Acme",)):
    docs = [eight_k(1, "5.02,9.01")]
    return eight_k_signals(docs, {1: {"8K-5.02": text}}, RULES, own)


def test_item_map_and_unmapped_items() -> None:
    out = eight_k_signals([eight_k(1, "1.03,2.02,9.01"), eight_k(2, "4.02")], {}, RULES)
    assert [(s.rule_id, s.severity, s.accession_no) for s in out] == [
        ("8k_1.03", "Critical", "acc-1"),
        ("8k_4.02", "Critical", "acc-2"),
    ]


# Sentences below are taken from (or modelled on) real 8-K Item 5.02 text.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "On January 7, 2025, Tammy Romo, Executive Vice President & Chief Financial Officer of "
            "Acme Co. (the “Company”), submitted her resignation from the position of "
            "Executive Vice President & Chief Financial Officer, effective April 1, 2025.",
            [("8k_5.02_cfo_departure", "Warning")],
        ),
        (
            "Thomas J. Edwards, Jr., 60, has been appointed Chief Operating Officer and Chief "
            "Financial Officer of Acme\u2019s, Inc., effective June 22, 2025.",
            [("8k_5.02_cfo_appointment", "Info")],
        ),
        (
            "Dave Denton notified the Company that he will step down from his current position as "
            "Chief Financial Officer effective August 15, 2026.",
            [("8k_5.02_cfo_departure", "Warning")],
        ),
        # Business-unit CEO, not the company's CEO.
        (
            "Piepszak, 54, Co-Chief Executive Officer of the Commercial & Investment Bank, has been "
            "named as a Chief Operating Officer of the Firm, effective immediately.",
            [("8k_5.02", "Info")],
        ),
        # Role mentioned, no change to it.
        (
            "Kathleen Johnson, who currently serves as President and Chief Executive Officer, will "
            "continue to serve as Chief Executive Officer.",
            [("8k_5.02", "Info")],
        ),
        # Biography of the appointee at another company.
        (
            "Mr. Stansbury previously served as the Senior Vice President and Chief Financial "
            "Officer of Arrow Electronics, Inc. from May 2016 through March 2022.",
            [("8k_5.02", "Info")],
        ),
        # CFO mentioned only as a reporting line for someone else's appointment.
        (
            "Prior to the effective date of his appointment, Mr. Crockett will serve as Vice "
            "President, Accounting of the Company reporting to Sherry House, the Company's Chief "
            "Financial Officer.",
            [("8k_5.02", "Info")],
        ),
    ],
)
def test_leadership_changes(text, expected) -> None:
    assert [(s.rule_id, s.severity) for s in run_502(text)] == expected


def test_departure_plus_appointment_is_succession() -> None:
    text = ("The Board appointed Jane Roe as Chief Financial Officer. "
            "John Doe will retire as Chief Financial Officer on March 1.")  # fmt: skip
    out = run_502(text)
    assert [(s.rule_id, s.severity, s.snippet) for s in out] == [
        ("8k_5.02_cfo_succession", "Warning",
         "John Doe will retire as Chief Financial Officer on March 1.")
    ]  # fmt: skip


def test_voluntary_listing_transfer_is_downgraded() -> None:
    body = ("Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or "
            "Standard; Transfer of Listing.\nOn September 25, 2026, the Company notified the "
            "NYSE of its intention to voluntarily de-list its common stock and transfer the "
            "listing to Nasdaq.")  # fmt: skip
    out = eight_k_signals([eight_k(1, "3.01")], {1: {"8K-3.01": body}}, RULES)
    assert [(s.severity, s.subcategory) for s in out] == [("Info", "Listing Transfer")]
    adverse = "Item 3.01 Notice of Delisting\nThe Company received notice from the NYSE that it is not in compliance."
    out = eight_k_signals([eight_k(1, "3.01")], {1: {"8K-3.01": adverse}}, RULES)
    assert [s.severity for s in out] == ["Warning"]


def test_sentences_do_not_split_on_abbreviations() -> None:
    assert sentences("Mr. A. Smith, Jr. resigned. Ms. Lee was named CFO.") == [
        "Mr. A. Smith, Jr. resigned.",
        "Ms. Lee was named CFO.",
    ]


def ten_k(doc_id=1, filed="2026-02-10"):
    return Doc(doc_id, "10-K", "PRIMARY", f"k-{doc_id}", filed, "2025-12-31", None)


@pytest.mark.parametrize(
    ("code", "text", "rule"),
    [
        (
            "10K-AUDIT",
            "The Company has suffered recurring losses that raise substantial doubt "
            "about its ability to continue as a going concern.",
            "going_concern",
        ),
        (
            "10K-9A",
            "Management identified a material weakness in internal control related to "
            "revenue recognition.",
            "material_weakness",
        ),
        (
            "10K-9A",
            "our disclosure controls and procedures were not effective as of December 31.",
            "controls_not_effective",
        ),
    ],
)
def test_keyword_rules_fire(code, text, rule) -> None:
    out = keyword_signals([ten_k()], {1: {code: text}}, RULES)
    assert [s.rule_id for s in out] == [rule]
    assert out[0].section_code == code and out[0].snippet


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("10K-9A", "Management did not identify any material weakness in internal control."),
        (
            "10K-AUDIT",
            "Management's plans alleviated the substantial doubt about the Company's "
            "ability to continue as a going concern.",
        ),
        (
            "10K-1A",
            "Such losses could raise substantial doubt about our ability to continue as a "
            "going concern.",
        ),  # risk-factor boilerplate is not in an audit report
    ],
)
def test_keyword_rules_skip_negations_and_other_sections(code, text) -> None:
    assert keyword_signals([ten_k()], {1: {code: text}}, RULES) == []


def test_auditor_tenure_and_change() -> None:
    old = auditor_info(ten_k(1, "2025-02-10"), "KPMG LLP", None)
    new = auditor_info(
        ten_k(2, "2026-02-10"),
        "Ernst & Young LLP",
        "Report of Independent Registered Public Accounting Firm\n"
        "We have served as the Company's auditor since 2025.",
    )
    out = auditor_signals([old, new])
    assert [(s.rule_id, s.severity, s.title) for s in out] == [
        ("auditor", "Info", "Auditor: Ernst & Young LLP (since 2025)"),
        ("auditor_change", "Warning", "Auditor changed from KPMG LLP to Ernst & Young LLP"),
    ]
    assert out[0].snippet == "We have served as the Company's auditor since 2025."
    same = auditor_info(ten_k(3, "2027-02-10"), "Ernst & Young, LLP", None)
    assert [s.rule_id for s in auditor_signals([new, same])] == ["auditor"]


def q(code, fy, fq, value, end="2026-06-30"):
    return {"metric_code": code, "period_type": "Q", "fiscal_year": fy, "fiscal_quarter": fq,
            "value": value, "period_end": end, "source_accn": "acc"}  # fmt: skip


def test_negative_equity_and_buybacks() -> None:
    values = [q("equity", 2026, 2, -4525e6)] + [
        q("buybacks", fy, fq, 1e9) for fy, fq in [(2025, 3), (2025, 4), (2026, 1), (2026, 2)]
    ]
    out = metric_signals(values, RULES)
    assert [s.rule_id for s in out] == ["negative_equity", "buybacks_with_negative_equity"]
    assert "-4,525M" in out[0].description and "4,000M" in out[1].description
    assert metric_signals([q("equity", 2026, 2, 10e9)], RULES) == []


def test_negative_equity_survives_a_preliminary_quarter() -> None:
    """Review 2026-10-14: a PRELIMINARY revenue row for the next quarter (earnings release
    before the 10-Q) made the latest quarter one without equity, and the signal flapped."""
    values = [q("equity", 2026, 2, -4525e6)] + [
        q("buybacks", fy, fq, 1e9) for fy, fq in [(2025, 3), (2025, 4), (2026, 1), (2026, 2)]
    ] + [q("revenue", 2026, 3, 7.3e9, end="2026-09-30")]  # fmt: skip
    out = metric_signals(values, RULES)
    assert [s.rule_id for s in out] == ["negative_equity", "buybacks_with_negative_equity"]
    assert "FY2026 Q2" in out[0].description
