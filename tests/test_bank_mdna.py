"""Phase 17d (D-060): bank MD&A. A 10-K filed as a wrapper whose text is the annual report in
Exhibit 13, and heading rules for filings without standard Item headings.

The filing index rows are those of Wells Fargo's 10-K filed 2026-02-24 (0000072971-26-000133)
and BNY's filed 2026-02-25 (EX-13.1); the document texts are short stand-ins with the same
headings.
"""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.extract.filings import Filing, exhibit_by_type
from uw_pipeline.jobs.run_documents import WRAPPER_ROLE, _filing_sections, _store_sections
from uw_pipeline.normalize.sections import apply_rules, split_items
from uw_pipeline.schema import company, filing_section, metadata, source_document

NOW = datetime(2026, 10, 21, tzinfo=UTC)
BODY = "x " * 2_000  # longer than a cross-reference
ACCN = "0000072971-26-000133"


def _index(rows: list[tuple[str, str, str]]) -> bytes:
    cells = "".join(
        f"<tr><td>{i}</td><td>{desc}</td><td><a href='/Archives/{name}'>{name}</a></td>"
        f"<td>{kind}</td></tr>"
        for i, (desc, name, kind) in enumerate(rows, 1)
    )
    return f"<html><table class='tableFile'>{cells}</table></html>".encode()


WFC_INDEX = _index([("FORM 10-K", "wfc-20251231_d2.htm", "10-K"),
                    ("EXHIBIT 13", "wfc-20251231.htm", "EX-13")])  # fmt: skip


def test_annual_report_exhibit_by_type() -> None:
    assert exhibit_by_type(WFC_INDEX, "EX-13", numbered=True) == "wfc-20251231.htm"
    bny = _index([("FORM 10-K", "bk-20251231.htm", "10-K"),
                  ("CERTAIN PORTIONS OF 2025 ANNUAL REPORT TO SHAREHOLDERS",
                   "bk-20251231_d2.htm", "EX-13.1")])  # fmt: skip
    assert exhibit_by_type(bny, "EX-13", numbered=True) == "bk-20251231_d2.htm"
    assert exhibit_by_type(bny, "EX-13") is None  # exact type unless numbered


def _rules(ticker: str) -> dict[str, tuple[str, str]]:
    reg = load_registry(CONFIG_DIR)
    return {c: (r.start, r.end) for c, r in reg.section_rules(ticker).items()}


def test_rules_skip_the_contents_and_page_headers() -> None:
    # Wells Fargo's annual report: contents first, then "Financial Review" with page headers.
    text = "\n".join([
        "Financial Review", "Overview", "Risk Factors", "Controls and Procedures",
        "Report of Independent Registered Public Accounting Firm",
        "Financial Review", "Overview", BODY, "Financial Review", BODY,
        "Risk Factors", BODY, "Controls and Procedures", "Disclosure Controls", "y " * 100,
        "Report of Independent Registered Public Accounting Firm", BODY,
    ])  # fmt: skip
    secs = {s.code: s for s in apply_rules(text, split_items(text, "10-K"), _rules("WFC"),
                                           "10-K")}  # fmt: skip
    mdna = secs["10K-7"].text
    assert mdna.startswith("Financial Review\nOverview\nx") and "\nFinancial Review\nx" in mdna
    assert secs["10K-1A"].text.startswith("Risk Factors\nx")
    assert secs["10K-9A"].text.startswith("Controls and Procedures\nDisclosure")
    assert "10Q-I-2" not in secs  # a 10-Q rule is not applied to a 10-K


def test_rule_section_replaces_the_misfiled_item() -> None:
    """Bank of America's 10-Q puts MD&A before its "Part I" heading: the split files it as
    Part II Item 2; the rule's section takes its place."""
    text = "\n".join([
        "Part I. Financial Information", "Item 2. Management's Discussion and Analysis 3",
        "Item 3. Quantitative and Qualitative Disclosures about Market Risk",
        "Part II. Other Information", "Item 1. Legal Proceedings",
        "Item 2. Management's Discussion and Analysis of Financial Condition", BODY,
        "Item 3. Quantitative and Qualitative Disclosures about Market Risk", "See MD&A.",
        "Part I. Financial Information", "Item 1. Financial Statements", BODY,
    ])  # fmt: skip
    split = {s.code: s for s in split_items(text, "10-Q")}
    assert len(split["10Q-II-2"].text) > len(BODY) and len(split["10Q-I-2"].text) < 100
    secs = {s.code: s for s in apply_rules(text, list(split.values()), _rules("BAC"), "10-Q")}
    assert BODY.strip() in secs["10Q-I-2"].text and "10Q-II-2" not in secs


class _Sec:
    """Recorded documents by name; counts the requests."""

    def __init__(self, docs: dict[str, bytes]) -> None:
        self.docs, self.requests = docs, []

    def filing_document(self, cik: str, accn: str, name: str) -> SimpleNamespace:
        self.requests.append(name)
        data = self.docs[name]
        return SimpleNamespace(payload=data, raw_path=f"sec/{accn}/{name}",
                               content_hash=hashlib.sha256(data).hexdigest())  # fmt: skip


def _html(*lines: str) -> bytes:
    return ("<html><body>" + "".join(f"<p>{x}</p>" for x in lines) + "</body></html>").encode()


WRAPPER = _html("Item 1. Business", "Wells Fargo & Company is a financial services company.",
                BODY, "Item 1A. Risk Factors", "See Risk Factors in the 2025 Annual Report.",
                "Item 7. Management's Discussion and Analysis",
                "See Financial Review in the 2025 Annual Report.",
                "Item 10. Directors", "See the proxy statement.")  # fmt: skip
REPORT = _html("Financial Review", "Overview", "Net interest margin was 2.65%.", BODY,
               "Risk Factors", BODY, "Controls and Procedures", "Disclosure controls", BODY,
               "Report of Independent Registered Public Accounting Firm", BODY)  # fmt: skip


def _db():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(company).values(
            company_id=1, cik="0000072971", ticker="WFC", name="Wells Fargo",
            legal_name="WELLS FARGO & COMPANY", industry_group="BANKS", is_financial=True,
            is_active=True, fiscal_year_end="1231", source_url="u", source_hash="h",
            raw_path="p", last_refreshed_at=NOW))  # fmt: skip
        c.execute(insert(source_document).values(
            doc_id=1, company_id=1, source_type="SEC_FILING", form_type="10-K",
            doc_role="PRIMARY", accession_no=ACCN, doc_name="wfc-20251231_d2.htm", url="u",
            filed_at="2026-02-24"))  # fmt: skip
    return engine


def test_wrapper_10k_reads_the_annual_report() -> None:
    from uw_pipeline.normalize.html_text import html_to_text

    engine = _db()
    sec = _Sec({f"{ACCN}-index.html": WFC_INDEX, "wfc-20251231.htm": REPORT})
    f = Filing(ACCN, "10-K", "2026-02-24", "2025-12-31", "wfc-20251231_d2.htm", True)
    with engine.begin() as c:
        _store_sections(c, 1, [])
        text = html_to_text(WRAPPER)
        doc_id, sections = _filing_sections(c, sec, 1, "0000072971", f, 1, text, _rules("WFC"),
                                            now=NOW)  # fmt: skip
        _store_sections(c, doc_id, sections)
        docs = {r.doc_name: r for r in c.execute(select(source_document))}
        stored = {r.section_code: r.text for r in c.execute(
            select(filing_section).where(filing_section.c.doc_id == doc_id))}  # fmt: skip
    # The annual report is now the filing's PRIMARY document; the 10-K is kept as the wrapper.
    assert docs["wfc-20251231.htm"].doc_role == "PRIMARY" and doc_id != 1
    assert docs["wfc-20251231_d2.htm"].doc_role == WRAPPER_ROLE
    assert docs["wfc-20251231.htm"].url.endswith(f"{ACCN.replace('-', '')}/wfc-20251231.htm")
    # MD&A, risk factors, controls from the annual report; Item 1 and 10 from the wrapper.
    assert "Net interest margin was 2.65%." in stored["10K-7"]
    assert stored["10K-1A"].startswith("Risk Factors\nx")
    assert stored["10K-1"].startswith("Item 1. Business") and "10K-10" in stored
    assert stored["10K-9A"].startswith("Controls and Procedures")


def test_10k_with_its_own_mdna_is_not_switched() -> None:
    from uw_pipeline.normalize.html_text import html_to_text

    engine = _db()
    sec = _Sec({})
    own = _html("Item 7. Management's Discussion and Analysis", BODY, "Item 8. Financial")
    f = Filing(ACCN, "10-K", "2026-02-24", "2025-12-31", "wfc-20251231_d2.htm", True)
    with engine.begin() as c:
        doc_id, sections = _filing_sections(c, sec, 1, "0000072971", f, 1, html_to_text(own),
                                            {}, now=NOW)  # fmt: skip
    assert doc_id == 1 and sec.requests == [] and {s.code for s in sections} >= {"10K-7"}


def test_bank_rules_load() -> None:
    for t in ("BAC", "BNY", "C", "MS", "USB", "WFC"):
        assert _rules(t), t
