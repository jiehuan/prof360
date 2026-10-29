"""D-069: MD&A history beyond the text window, from 10-Ks (years) and 10-Qs (quarters)."""

from datetime import UTC, date, datetime

import httpx
import respx
from sqlalchemy import create_engine, insert, select

from tests.conftest import CONFIG_DIR
from uw_pipeline.clients.sec import SEC_DATA, archive_folder_url
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig, load_industry_config
from uw_pipeline.jobs.run_documents import run_documents_stage
from uw_pipeline.schema import company, filing_section, metadata, source_document
from uw_pipeline.signals.rules import load_signal_rules

CIK = 43
TODAY = date(2026, 10, 13)  # text window (400 days) from 2025-09-08; 8 quarters from 2024-10-07
FILLER = "<p>" + "Discussion of results. " * 60 + "</p>"
TEN_K = f"""<html><body>
<p>ITEM 1A. RISK FACTORS</p>{FILLER}<p>ITEM 3. LEGAL PROCEEDINGS</p><p>None.</p>
<p>ITEM 7. MANAGEMENT&#8217;S DISCUSSION AND ANALYSIS</p>{FILLER}
<p>ITEM 8. FINANCIAL STATEMENTS</p>{FILLER}</body></html>"""
TEN_Q = (
    f"<html><body><p>PART I</p><p>Item 2. MD&amp;A</p>{FILLER}<p>Item 4. Controls</p>"
    f"{FILLER}<p>PART II</p><p>Item 1. Legal Proceedings</p><p>None.</p></body></html>"
)

FILINGS = [
    # accn, form, filed, report, primary
    ("0000000043-21-000001", "10-K", "2021-02-10", "2020-12-31", "k20.htm"),  # > 5 years: no
    ("0000000043-24-000002", "10-Q", "2024-05-06", "2024-03-31", "q24a.htm"),  # > 8 quarters: no
    ("0000000043-25-000003", "10-K", "2025-02-11", "2024-12-31", "k24.htm"),  # history
    ("0000000043-25-000004", "10-Q", "2025-05-06", "2025-03-31", "q25a.htm"),  # history
    ("0000000043-25-000005", "10-Q", "2025-11-04", "2025-09-30", "q25c.htm"),  # text window
]


def _submissions() -> dict:
    cols = ["accessionNumber", "form", "filingDate", "reportDate", "primaryDocument"]
    recent = {c: [f[i] for f in FILINGS] for i, c in enumerate(cols)}
    recent["items"] = [""] * len(FILINGS)
    recent["isXBRL"] = [0] * len(FILINGS)
    return {"cik": "43", "filings": {"recent": recent, "files": []}}


def _run(make_sec_client, history):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(company).values(
            cik="0000000043", ticker="HOTL", name="Hotel Co", legal_name="HOTEL CO",
            sic_code="7011", sic_description="Hotels & Motels", industry_group="HOTELS",
            is_financial=False, is_active=True, source_url="u", source_hash="h", raw_path="p",
            last_refreshed_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))  # fmt: skip
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{SEC_DATA}/submissions/CIK0000000043.json").mock(
            return_value=httpx.Response(200, json=_submissions())
        )
        for accn, form, *_, name in FILINGS:
            folder = archive_folder_url(CIK, accn)
            body = (TEN_K if form == "10-K" else TEN_Q).encode()
            files = {name: body, f"{accn}-index.html": b"<table class='tableFile'></table>"}
            router.get(f"{folder}/index.json").mock(return_value=httpx.Response(
                200, json={"directory": {"item": [{"name": n} for n in files]}}))  # fmt: skip
            for n, b in files.items():
                router.get(f"{folder}/{n}").mock(return_value=httpx.Response(200, content=b))
        res = run_documents_stage(
            [CompanyConfig(ticker="HOTL", cik=CIK)], make_sec_client(day=TODAY),
            load_registry(CONFIG_DIR), load_signal_rules(CONFIG_DIR), engine,
            today=lambda: TODAY, history=history,
        )  # fmt: skip
    assert res[0].ok, res
    with engine.connect() as conn:
        rows = conn.execute(
            select(source_document.c.accession_no, filing_section.c.section_code)
            .join(filing_section, filing_section.c.doc_id == source_document.c.doc_id)
        ).all()  # fmt: skip
    sections: dict[str, set[str]] = {}
    for accn, code in rows:
        sections.setdefault(accn, set()).add(code)
    return res[0].detail, sections


def test_history_keeps_only_the_mdna_of_older_10ks_and_10qs(make_sec_client) -> None:
    detail, sections = _run(make_sec_client, lambda cfg, group: (5, 8))
    assert "history_10k=1" in detail and "history_10q=1" in detail
    assert sections["0000000043-25-000003"] == {"10K-7"}  # older 10-K: MD&A only
    assert sections["0000000043-25-000004"] == {"10Q-I-2"}  # older 10-Q: MD&A only
    assert len(sections["0000000043-25-000005"]) > 1  # text window: every section
    assert "0000000043-21-000001" not in sections  # beyond 5 years
    assert "0000000043-24-000002" not in sections  # beyond 8 quarters


def test_without_history_only_the_text_window(make_sec_client) -> None:
    detail, sections = _run(make_sec_client, None)
    assert "history_10k" not in detail
    assert set(sections) == {"0000000043-25-000005"}


def test_default_history_for_companies_with_text_metrics() -> None:
    ind = load_industry_config(CONFIG_DIR)
    assert ind.mdna_history(ind.groups["HOTELS"], True) == (5, 8)
    assert ind.mdna_history(ind.groups["HOTELS"], False) == (0, 0)
    assert ind.mdna_history(ind.groups["BANKS"], True) == (5, 8)  # its own 5 years
    override = ind.groups["HOTELS"].model_copy(update={"mdna_history_quarters": 0})
    assert ind.mdna_history(override, True) == (5, 0)
