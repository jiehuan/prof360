"""documents + signals stages end to end on a synthetic filer (all HTTP mocked)."""

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy import create_engine, func, insert, select

from tests.conftest import CONFIG_DIR
from tests.fake_llm import NOTHING, FakeLLM, signals
from uw_pipeline.clients.sec import SEC_DATA, archive_folder_url
from uw_pipeline.compute.registry import load_registry
from uw_pipeline.config import CompanyConfig
from uw_pipeline.enrich.llm import load_llm_config
from uw_pipeline.jobs.run_documents import _store_sections, run_documents_stage
from uw_pipeline.jobs.run_signals import run_signals_stage
from uw_pipeline.normalize.sections import Section
from uw_pipeline.schema import (
    company,
    filing_section,
    metadata,
    section_chunk,
    signal,
    signal_vote,
    source_document,
)
from uw_pipeline.signals.rules import load_signal_rules

CIK = 42
TODAY = date(2026, 10, 13)
FILLER = "<p>" + "Discussion of results. " * 60 + "</p>"

TEN_K = f"""<html><body>
<p>Item 1A. Risk Factors 10</p><p>Item 3. Legal Proceedings 20</p><p>Item 7. MD&amp;A 30</p>
<p>ITEM 1A. RISK FACTORS</p>{FILLER}
<p>ITEM 3. LEGAL PROCEEDINGS</p><p>Refer to Note 12.</p>
<p>ITEM 7. MANAGEMENT&#8217;S DISCUSSION AND ANALYSIS</p>{FILLER}
<p>ITEM 9A. CONTROLS AND PROCEDURES</p>
<p>Management identified a material weakness in internal control over financial reporting.</p>
<p>Report of Independent Registered Public Accounting Firm</p>
<p>We have served as the Company&#8217;s auditor since 2011.</p><p>/s/ Big Four LLP</p>
<p>Chicago, Illinois</p><p>February 10, 2026</p>
</body></html>"""
TEN_Q = f"<html><body><p>PART I</p><p>Item 2. MD&amp;A</p>{FILLER}<p>PART II</p><p>Item 1. Legal Proceedings</p><p>None.</p></body></html>"
EIGHT_K_502 = """<html><body><p>Item 5.02 Departure of Directors or Certain Officers</p>
<p>On June 1, 2026, Pat Lee notified Acme Corp (the &#8220;Company&#8221;) that she will retire as Chief Financial Officer effective July 31, 2026.</p>
<p>Item 9.01 Financial Statements and Exhibits</p></body></html>"""
INDEX_HTML = """<html><body><table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/42/x/acme-8k.htm">acme-8k.htm</a></td><td>8-K</td><td>1</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/42/x/er-q2.htm">er-q2.htm</a></td><td>EX-99.1</td><td>1</td></tr>
</table></body></html>"""
INSTANCE = b"""<?xml version="1.0"?><xbrl xmlns="http://www.xbrl.org/2003/instance"
 xmlns:dei="http://xbrl.sec.gov/dei/2025"><context id="c"><entity><identifier scheme="x">42</identifier></entity>
 <period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period></context>
 <dei:AuditorName contextRef="c">Big Four LLP</dei:AuditorName></xbrl>"""

FILINGS = [
    # accn, form, filed, report, primary, items
    ("0000000042-26-000001", "10-K", "2026-02-10", "2025-12-31", "acme-10k.htm", ""),
    ("0000000042-26-000002", "10-K/A", "2026-03-01", "2025-12-31", "acme-10ka.htm", ""),
    ("0000000042-26-000003", "10-Q", "2026-05-05", "2026-03-31", "acme-10q.htm", ""),
    ("0000000042-26-000004", "8-K", "2026-06-02", "", "acme-502.htm", "5.02"),
    ("0000000042-26-000005", "8-K", "2026-07-30", "", "acme-8k.htm", "2.02,9.01"),
    ("0000000042-26-000006", "4", "2026-07-31", "", "form4.xml", ""),
]


def _submissions() -> dict:
    cols = ["accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "items"]
    recent = {c: [f[i] for f in FILINGS] for i, c in enumerate(cols)}
    recent["isXBRL"] = [1] * len(FILINGS)
    return {"cik": "42", "filings": {"recent": recent, "files": []}}


@pytest.fixture
def env(make_sec_client):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(company).values(
                cik="0000000042",
                ticker="ACME",
                name="Acme",
                legal_name="ACME CORP",
                sic_code="3711",
                sic_description="x",
                industry_group="AUTOMOTIVE",
                is_financial=False,
                is_active=True,
                source_url="u",
                source_hash="h",
                raw_path="p",
                last_refreshed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    docs = {
        "0000000042-26-000001": {"acme-10k.htm": TEN_K.encode(), "acme-10k_htm.xml": INSTANCE},
        "0000000042-26-000002": {"acme-10ka.htm": b"<p>Part III amendment</p>"},
        "0000000042-26-000003": {"acme-10q.htm": TEN_Q.encode()},
        "0000000042-26-000004": {"acme-502.htm": EIGHT_K_502.encode()},
        "0000000042-26-000005": {
            "0000000042-26-000005-index.html": INDEX_HTML.encode(),
            "er-q2.htm": b"<p>Acme reports second quarter results. Revenue $1.2 billion.</p>",
        },
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{SEC_DATA}/submissions/CIK0000000042.json").mock(
            return_value=httpx.Response(200, json=_submissions())
        )
        for accn, files in docs.items():
            folder = archive_folder_url(CIK, accn)
            names = {"directory": {"item": [{"name": n} for n in files]}}
            router.get(f"{folder}/index.json").mock(return_value=httpx.Response(200, json=names))
            for name, body in files.items():
                router.get(f"{folder}/{name}").mock(return_value=httpx.Response(200, content=body))
        yield engine, make_sec_client(day=TODAY), router


def _run(engine, sec, llm=None):
    cfg = [CompanyConfig(ticker="ACME", cik=CIK)]
    registry, rules = load_registry(CONFIG_DIR), load_signal_rules(CONFIG_DIR)
    res = run_documents_stage(cfg, sec, registry, rules, engine, today=lambda: TODAY)
    llm_pair = (llm, load_llm_config(CONFIG_DIR)) if llm else None
    res += run_signals_stage(cfg, sec, rules, engine, llm=llm_pair)
    assert all(r.ok for r in res), res
    return res


def _fake_llm():
    def answer(model, user):
        if "8K-5.02" in user:
            return signals(leadership=[("CFO", "departure", "Pat Lee",
                                        "she will retire as Chief Financial Officer")])  # fmt: skip
        if "10K-9A" in user:
            return signals(findings=[("material_weakness", "identified a material weakness")])
        return NOTHING

    return FakeLLM(answer)


def test_signals_with_llm_vote_and_no_calls_on_rerun(env) -> None:
    engine, sec, _ = env
    llm = _fake_llm()
    _run(engine, sec, llm)
    first_calls = len(llm.calls)
    with engine.connect() as conn:
        sigs = {r.rule_id: r for r in conn.execute(select(signal))}
        votes = {(r.topic, r.status) for r in conn.execute(select(signal_vote))}
    # 10-K (9A + audit report) and the 5.02 8-K; the 10-Q has no Item 4 in scope. LLM agrees
    # with the rules everywhere, so there are no tie-breaks.
    assert first_calls == 2
    assert sigs["8k_5.02_cfo_departure"].review_status == "UNANIMOUS"
    assert sigs["material_weakness"].method == "VOTE"
    assert sigs["auditor"].review_status == "RULE_ONLY"
    assert ("leadership:CFO", "UNANIMOUS") in votes
    _run(engine, sec, llm)
    assert len(llm.calls) == first_calls  # inputs unchanged: every answer comes from the cache


def test_llm_auth_failure_falls_back_to_rules(env) -> None:
    import anthropic
    import httpx as _httpx

    engine, sec, _ = env
    req = _httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError(
        "bad key", response=_httpx.Response(401, request=req), body=None
    )
    _run(engine, sec, FakeLLM(lambda m, u: err))
    with engine.connect() as conn:
        statuses = {r.review_status for r in conn.execute(select(signal))}
    assert statuses == {"RULE_ONLY"}


def test_documents_and_signals(env) -> None:
    engine, sec, _ = env
    _run(engine, sec)
    with engine.connect() as conn:
        docs = {
            (r.accession_no[-2:], r.form_type, r.doc_role): r.content_hash is not None
            for r in conn.execute(select(source_document))
        }
        sections = {
            (r.form_type, r.section_code)
            for r in conn.execute(
                select(source_document.c.form_type, filing_section.c.section_code).join(
                    filing_section, filing_section.c.doc_id == source_document.c.doc_id
                )
            )
        }
        sigs = {r.rule_id: r for r in conn.execute(select(signal))}
    # Form 4 is not registered; the 10-K/A is registered but not downloaded or split.
    assert docs == {
        ("01", "10-K", "PRIMARY"): True,
        ("02", "10-K/A", "PRIMARY"): False,
        ("03", "10-Q", "PRIMARY"): True,
        ("04", "8-K", "PRIMARY"): True,  # 5.02: body downloaded for the leadership rules
        ("05", "8-K", "PRIMARY"): False,  # 2.02: only its press release is needed
        ("05", "8-K", "EX-99.1"): True,
    }
    assert {
        ("10-K", "10K-1A"), ("10-K", "10K-3"), ("10-K", "10K-7"), ("10-K", "10K-9A"),
        ("10-K", "10K-AUDIT"), ("10-Q", "10Q-I-2"), ("10-Q", "10Q-II-1"), ("8-K", "8K-5.02"),
    } <= sections  # fmt: skip
    assert set(sigs) == {"auditor", "material_weakness", "8k_5.02_cfo_departure"}
    assert sigs["auditor"].title == "Auditor: Big Four LLP (since 2011)"
    assert sigs["material_weakness"].section_code == "10K-9A"
    assert sigs["8k_5.02_cfo_departure"].severity == "Warning"
    assert sigs["8k_5.02_cfo_departure"].doc_id is not None


def test_rerun_is_idempotent(env) -> None:
    engine, sec, _ = env
    _run(engine, sec)

    def snapshot():
        with engine.connect() as conn:
            return (
                conn.execute(select(func.count()).select_from(source_document)).scalar_one(),
                conn.execute(select(func.count()).select_from(filing_section)).scalar_one(),
                sorted(
                    conn.execute(
                        select(signal.c.signal_id, signal.c.signal_key, signal.c.detected_at)
                    ).all()
                ),
            )

    before = snapshot()
    _run(engine, sec)
    assert snapshot() == before


def test_resplit_removes_sections_no_longer_found(env) -> None:
    engine, sec, _ = env
    _run(engine, sec)
    with engine.begin() as conn:
        doc_id = conn.execute(
            select(source_document.c.doc_id).where(source_document.c.form_type == "10-K")
        ).scalar_one()
        _store_sections(conn, doc_id, [Section("10K-1A", 0, 9, "Risk text")])
        codes = (
            conn.execute(
                select(filing_section.c.section_code).where(filing_section.c.doc_id == doc_id)
            )
            .scalars()
            .all()
        )
        orphans = conn.execute(
            select(func.count()).select_from(section_chunk).where(
                section_chunk.c.section_id.not_in(select(filing_section.c.section_id)))
        ).scalar_one()  # fmt: skip
    assert codes == ["10K-1A"] and orphans == 0
